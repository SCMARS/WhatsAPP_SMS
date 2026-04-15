"""
ElevenLabs integration — ConvAI WebSocket (text-only) + STT (Scribe).

Conversation continuity:
  - One persistent WebSocket per WhatsApp chat (keyed by phone number).
  - The socket is only replaced when ElevenLabs closes it server-side.
  - We NEVER proactively close a working socket — ElevenLabs does it on their
    own schedule (~5-10 min of idle). On reconnect we get a new conversation
    context, but history is re-injected as contextual_update so the agent
    stays grounded.
"""
import asyncio
import json
import logging
import random
import re
import ssl
import time
import uuid
from typing import Optional

import certifi
import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from app.config import settings

logger = logging.getLogger(__name__)

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"

# Limit concurrent WebSocket connections to ElevenLabs to avoid DNS/connection failures
# during bulk sends (each lead opens 2 WS connections due to warmup→reconnect).
_WS_SEMAPHORE = asyncio.Semaphore(5)

WHATSAPP_CONTEXT = (
    "This is a WhatsApp text chat. Keep your reply short — max 3-4 sentences. "
    "Plain text only, no markdown."
)

STYLE_HINTS = [
    "Start with a question.",
    "Open with the bonus offer directly.",
    "Start with a compliment to the player.",
    "Open with urgency — limited time.",
    "Start very casually, like you haven't talked in a while.",
    "Lead with the link, explain after.",
    "Start with a personal greeting.",
    "Open with surprise — act like you have exclusive news.",
    "Begin with a short emoji.",
    "Start by mentioning the player's name naturally.",
]


def _ssl_ctx() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


def _el_headers() -> dict:
    return {"xi-api-key": settings.ELEVENLABS_API_KEY}


async def _get_signed_url(agent_id: str) -> str:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{ELEVENLABS_BASE}/convai/conversation/get_signed_url",
            headers=_el_headers(),
            params={"agent_id": agent_id},
        )
        resp.raise_for_status()
        return resp.json()["signed_url"]


def _normalize_dynamic_variables(dynamic_variables: Optional[dict[str, str]]) -> dict[str, str]:
    if not dynamic_variables:
        return {}
    return {str(k): str(v) for k, v in dynamic_variables.items() if v is not None}


def _substitute_dynamic_placeholders(
    text: str,
    *,
    language: Optional[str] = None,
    link_url: Optional[str] = None,
    promo_code: Optional[str] = None,
) -> str:
    result = text or ""
    replacements = {
        "language": language or "",
        "link": link_url or "",
        "promo": promo_code or "",
    }
    for key, value in replacements.items():
        result = re.sub(rf"\{{\{{\s*{re.escape(key)}\s*\}}\}}", value, result, flags=re.IGNORECASE)
        result = re.sub(rf"\{{\s*{re.escape(key)}\s*\}}", value, result, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", result).strip()


def _missing_required_fields(
    text: str,
    *,
    link_url: Optional[str] = None,
    promo_code: Optional[str] = None,
) -> list[str]:
    missing: list[str] = []
    if link_url and link_url not in text:
        missing.append("link")
    if promo_code and promo_code not in text:
        missing.append("promo")
    return missing


def _clickability_trigger(language: str) -> str:
    """Randomized instruction to encourage a reply so the link becomes clickable."""
    if language == "es-AR":
        return random.choice([
            "El link se va a poder clickear si mandás cualquier mensaje en este chat (incluso un emoji) 🙂 ¡Buena suerte! 🤞",
            "Mandame cualquier mensaje o emoji para activar el link. ¡Buena suerte! 🤞",
            "Respondé con cualquier cosa y el link queda activo al toque. ¡Mucha suerte! 🍀",
        ])
    # Default: pt-PT
    return random.choice([
        "O link ficará clicável se enviares qualquer mensagem neste chat (mesmo um emoji) 🙂 Boa sorte 🤞",
        "Envia qualquer mensagem ou emoji e o link ativa-se na hora. Boa sorte! 🤞",
        "Responde com qualquer coisa para o link ficar clicável. Boa sorte! 🍀",
    ])


def _missing_fields_tail(
    language: str,
    *,
    missing_fields: list[str],
    link_url: Optional[str] = None,
    promo_code: Optional[str] = None,
) -> str:
    if not missing_fields:
        return ""

    if language == "es-AR":
        parts = []
        if "promo" in missing_fields and promo_code:
            parts.append(random.choice([
                f"Aprovechá con el código: {promo_code}.",
                f"Tu código de bono es {promo_code}.",
                f"Usá el código {promo_code} para activar el regalo.",
            ]))
        if "link" in missing_fields and link_url:
            parts.append(random.choice([
                f"Acá tenés el link: {link_url}",
                f"Entrá por acá para jugar: {link_url}",
                f"Link de activación: {link_url}",
            ]))
        return " ".join(parts).strip()

    # Default: pt-PT
    parts = []
    if "promo" in missing_fields and promo_code:
        parts.append(random.choice([
            f"Usa o teu código: {promo_code}.",
            f"O teu bónus ativa-se com o código {promo_code}.",
            f"Código promocional: {promo_code}.",
        ]))
    if "link" in missing_fields and link_url:
        parts.append(random.choice([
            f"Aqui tens o link para começar: {link_url}",
            f"Usa este link de ativação: {link_url}",
            f"Segue este link: {link_url}",
        ]))
    return " ".join(parts).strip()


def _ensure_required_outreach_fields(
    text: str,
    language: str,
    *,
    link_url: Optional[str] = None,
    promo_code: Optional[str] = None,
) -> str:
    """
    1. Substitutes placeholders.
    2. Appends missing mandatory fields (link/promo) naturally.
    3. Appends a 'clickability trigger' instruction at the very end.
    """
    result = _substitute_dynamic_placeholders(
        text,
        language=language,
        link_url=link_url,
        promo_code=promo_code,
    )
    missing_fields = _missing_required_fields(
        result,
        link_url=link_url,
        promo_code=promo_code,
    )
    
    # Add link/promo if missing
    if missing_fields:
        tail = _missing_fields_tail(
            language,
            missing_fields=missing_fields,
            link_url=link_url,
            promo_code=promo_code,
        )
        if tail:
            result = f"{result} {tail}".strip()
            
    # Only add the activation trigger if ElevenLabs didn't already include one
    if result and "emoji" not in result.lower():
        trigger = _clickability_trigger(language)
        result = f"{result} {trigger}".strip()
    return result or _clickability_trigger(language)


def _fallback_outreach(language: str, link_url: str, promo_code: Optional[str]) -> str:
    promo = promo_code or "50Pragmatic"
    if language == "es-AR":
        return (
            "¡Hola! Soy Olivia de Pampas 🙂 Fue un placer charlar con vos. "
            f"Como te prometí, acá tenés el link de tu bono del 175% en tu próximo depósito desde ARS 5000 · solo 5 días 👉 {link_url} "
            "El link se va a poder clickear si mandás cualquier mensaje en este chat (incluso un emoji) 🙂 ¡Buena suerte! 🤞"
        )
    return (
        f"Olá! Sou a Camila do Oro Casino 🙂 Foi um prazer falar contigo. "
        f"Como prometi, aqui está o teu código promocional: {promo} — 50 Rodadas Grátis no Pragmatic Play · apenas 5 dias 👉 {link_url} "
        "O link ficará clicável se enviares qualquer mensagem neste chat (mesmo um emoji) 🙂 Boa sorte 🤞"
    )


async def _open_socket(
    agent_id: str,
    language: Optional[str] = None,
    dynamic_variables: Optional[dict[str, str]] = None,
) -> websockets.WebSocketClientProtocol:
    """Open a fresh ElevenLabs ConvAI WebSocket and complete the handshake."""
    signed_url = await _get_signed_url(agent_id)
    ws = await websockets.connect(
        signed_url,
        ssl=_ssl_ctx(),
        open_timeout=20,
        close_timeout=10,
        ping_interval=None,   # let ElevenLabs drive keepalive
        ping_timeout=None,
    )

    # 1. Wait for server's initiation metadata
    async for raw in ws:
        msg = json.loads(raw)
        if msg.get("type") == "conversation_initiation_metadata":
            logger.debug("ElevenLabs WS: got initiation metadata")
            break

    # 2. Enable text-only mode + language override
    config_override: dict = {"conversation": {"text_only": True}}
    if language:
        # Tell ElevenLabs which language the agent should respond in
        config_override["agent"] = {"language": language}

    payload = {
        "type": "conversation_initiation_client_data",
        "conversation_config_override": config_override,
    }
    normalized_vars = _normalize_dynamic_variables(dynamic_variables)
    if normalized_vars:
        payload["dynamic_variables"] = normalized_vars

    await ws.send(json.dumps(payload))

    return ws


def _is_socket_alive(ws) -> bool:
    """Return True only if the socket object exists and is still open."""
    if ws is None:
        return False
    
    # Handle newer websockets versions (14.0+)
    if hasattr(ws, "state"):
        # state is an Enum like State.OPEN (value 1)
        state_name = getattr(ws.state, "name", "")
        state_val = getattr(ws.state, "value", -1)
        return state_name == "OPEN" or state_val == 1
        
    # Handle legacy websockets versions
    return not getattr(ws, "closed", True)


class _ChatSession:
    """One persistent WS session per WhatsApp phone number."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._language: Optional[str] = None
        self._dynamic_vars: dict[str, str] = {}

    async def _ensure_connected(
        self,
        agent_id: str,
        language: Optional[str] = None,
        dynamic_variables: Optional[dict[str, str]] = None,
    ) -> websockets.WebSocketClientProtocol:
        normalized_vars = _normalize_dynamic_variables(dynamic_variables)
        # If language changed, force reconnect to use new config
        if (
            _is_socket_alive(self.ws)
            and self._language == language
            and self._dynamic_vars == normalized_vars
        ):
            return self.ws
        if _is_socket_alive(self.ws) and (
            self._language != language or self._dynamic_vars != normalized_vars
        ):
            logger.info("WS config changed, reconnecting (language and/or dynamic vars)")
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass

        logger.info(f"Opening new ElevenLabs WS (agent={agent_id})")
        self.ws = await _open_socket(
            agent_id,
            language=language,
            dynamic_variables=normalized_vars,
        )
        self._language = language
        self._dynamic_vars = normalized_vars
        return self.ws

    async def ask(
        self,
        agent_id: str,
        context_text: str,
        prior_turns: list[dict],
        last_user_text: str,
        language: Optional[str] = None,
        dynamic_variables: Optional[dict[str, str]] = None,
    ) -> str:
        async with self.lock:
            reply = ""

            ws = await self._ensure_connected(
                agent_id,
                language=language,
                dynamic_variables=dynamic_variables,
            )

            # --- DRAIN BUFFER ---
            # Anything sitting in the buffer now (like an automated ElevenLabs greeting)
            # belongs to a previous turn or connection greeting. Clear it.
            try:
                while True:
                    # Give it a bit more time to catch the initial "Hey" (1.0s)
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    msg = json.loads(raw)
                    if msg.get("type") == "agent_response":
                        txt = msg.get("agent_response_event", {}).get("agent_response", "")
                        logger.info(f"ElevenLabs WS: drained/discarded old greeting: '{txt}'")
                    else:
                        logger.debug(f"ElevenLabs WS: drained msg type='{msg.get('type')}'")
            except asyncio.TimeoutError:
                # Buffer is clean
                pass
            except Exception as e:
                logger.debug(f"Stop draining buffer: {e}")

            # --- TURN HANDLERS ---
            async def _send_turn(w: websockets.WebSocketClientProtocol) -> None:
                # Re-inject history so agent stays grounded on reconnect
                if prior_turns:
                    history_text = "\n".join(
                        f"{'User' if m['role'] == 'user' else 'Agent'}: {m['content']}"
                        for m in prior_turns
                    )
                    await w.send(json.dumps({
                        "type": "contextual_update",
                        "text": f"Previous conversation:\n{history_text}",
                    }))

                await w.send(json.dumps({
                    "type": "contextual_update",
                    "text": context_text,
                }))

                await w.send(json.dumps({
                    "type": "user_message",
                    "text": last_user_text,
                }))

            async def _collect(w: websockets.WebSocketClientProtocol) -> None:
                nonlocal reply
                async for raw in w:
                    msg = json.loads(raw)
                    t = msg.get("type", "")

                    if t == "ping":
                        eid = msg.get("ping_event", {}).get("event_id")
                        await w.send(json.dumps({"type": "pong", "event_id": eid}))
                        continue

                    if t == "agent_response":
                        text = msg.get("agent_response_event", {}).get("agent_response", "")
                        if text:
                            reply = text.strip()
                            return
                        else:
                            continue

                    if t == "internal_error":
                        logger.error(f"ElevenLabs internal error: {msg}")
                        return

            async def _run_turn(w: websockets.WebSocketClientProtocol) -> None:
                await _send_turn(w)
                await asyncio.wait_for(_collect(w), timeout=45)

            try:
                await _run_turn(ws)
            except (ConnectionClosed, ConnectionError, OSError) as e:
                logger.warning(f"ElevenLabs WS closed mid-turn ({e}), reconnecting…")
                self.ws = None
                ws = await self._ensure_connected(
                    agent_id,
                    language=language,
                    dynamic_variables=dynamic_variables,
                )
                reply = ""
                await _run_turn(ws)
            except asyncio.TimeoutError:
                logger.warning("ElevenLabs _collect timed out after 45s — resetting socket")
                self.ws = None  # Force reconnect on next turn; stale response must not leak through
                raise

            return reply

    async def reset(self) -> None:
        async with self.lock:
            if self.ws is not None:
                try:
                    await self.ws.close()
                except Exception:
                    pass
            self.ws = None
            self._dynamic_vars = {}


# ---------------------------------------------------------------------------
# Global session registry  (one session = one WhatsApp phone)
# ---------------------------------------------------------------------------

_SESSIONS: dict[str, _ChatSession] = {}


def _get_session(chat_key: str) -> _ChatSession:
    s = _SESSIONS.get(chat_key)
    if s is None:
        s = _ChatSession()
        _SESSIONS[chat_key] = s
    return s


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def generate_text_reply(
    agent_id: str,
    system_prompt: str,
    history: list[dict],
    lead_name: Optional[str] = None,
    chat_key: Optional[str] = None,
    language: Optional[str] = None,
    dynamic_variables: Optional[dict[str, str]] = None,
) -> str:
    """
    Generate a text reply via ElevenLabs ConvAI WebSocket (text-only mode).
    One persistent socket per WhatsApp chat. Reconnects automatically only
    when ElevenLabs closes the connection.
    language: ISO code like 'pt', 'es', 'en' — passed to ElevenLabs agent config override.
    """
    user_messages = [m for m in history if m.get("role") == "user"]
    if not user_messages:
        logger.warning("No user messages in history")
        return ""

    last_user_text = user_messages[-1]["content"]
    prior_turns = history[:-1]

    context_parts = []
    if lead_name:
        context_parts.append(f"Customer name: {lead_name}.")
    context_parts.append(WHATSAPP_CONTEXT)
    style_hint = random.choice(STYLE_HINTS)
    uid = str(uuid.uuid4())[:8]
    context_parts.append(f"Style for this message: {style_hint} [uid:{uid}]")
    context_text = " ".join(context_parts)

    key = chat_key or agent_id
    session = _get_session(key)

    try:
        reply = await session.ask(
            agent_id=agent_id,
            context_text=context_text,
            prior_turns=prior_turns,
            last_user_text=last_user_text,
            language=language,
            dynamic_variables=dynamic_variables,
        )
    except asyncio.TimeoutError:
        logger.warning("ElevenLabs WebSocket timeout after 45s")
        return ""
    except Exception as e:
        err = str(e).lower()
        if any(code in err for code in ("1011", "1002", "1000", "connectionclosed", "failed to generate")):
            logger.warning(f"ElevenLabs transient socket error, hard-reset and retry: {e}")
            await session.reset()
            try:
                reply = await session.ask(
                    agent_id=agent_id,
                    context_text=context_text,
                    prior_turns=prior_turns,
                    last_user_text=last_user_text,
                    language=language,
                    dynamic_variables=dynamic_variables,
                )
            except asyncio.TimeoutError:
                logger.warning("ElevenLabs retry timed out")
                return ""
            except Exception as retry_err:
                logger.error(f"ElevenLabs retry failed: {retry_err}")
                return ""
        else:
            import traceback
            logger.error(f"ElevenLabs generate_text_reply failed:\n{traceback.format_exc()}")
            return ""

    logger.info(f"ElevenLabs reply ({len(reply)} chars): {reply[:120]}")
    return reply


async def get_agent_prompt(agent_id: str) -> dict:
    """Fetch full agent config from ElevenLabs. Returns prompt + first_message."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{ELEVENLABS_BASE}/convai/agents/{agent_id}",
            headers=_el_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    try:
        agent_cfg = data["conversation_config"]["agent"]
        prompt = agent_cfg["prompt"]["prompt"]
        first_message = agent_cfg.get("first_message", "")
    except (KeyError, TypeError) as e:
        logger.warning(f"Could not extract prompt fields from agent config: {e}")
        prompt = ""
        first_message = ""

    return {"prompt": prompt, "first_message": first_message}


async def get_outreach_message(
    agent_id: str,
    link_url: Optional[str] = None,
    promo_code: Optional[str] = None,
) -> str:
    """
    Fetch the agent's first_message from ElevenLabs (REST, no WebSocket) and
    substitute link/promo placeholders.

    Configure your ElevenLabs agent's "First message" field with placeholders:
        {link}   → replaced with the affiliate link
        {promo}  → replaced with the promo code

    Example first_message in ElevenLabs dashboard:
        "Привет! Это Анна из Vivajack 🙂 Вот твоя персональная ссылка:
         {link}  Промокод: {promo}. Ответь на это сообщение чтобы активировать!"

    Returns the final message string, or empty string if first_message not set.
    """
    config = await get_agent_prompt(agent_id)
    template = config.get("first_message", "").strip()

    if not template or template.lower() in ("hey", "hello", "hi", ""):
        logger.warning(
            "ElevenLabs first_message is empty or default ('%s'). "
            "Set it in the ElevenLabs dashboard → Agent → First message.",
            template,
        )
        return ""

    # Substitute placeholders — support both {link}/{promo} and {{link}}/{{promo}}
    result = _ensure_required_outreach_fields(
        template,
        language="pt-PT",
        link_url=link_url,
        promo_code=promo_code,
    )

    logger.info("ElevenLabs outreach message fetched (%d chars)", len(result))
    return result


async def generate_outreach_message(
    agent_id: str,
    chat_key: str,
    language: str,
    link_url: str,
    promo_code: Optional[str] = None,
) -> str:
    """
    Generate initial outbound message via ElevenLabs ConvAI WebSocket.
    Uses dynamic variables consumed by the agent prompt: {language}, {link}, {promo}.
    """
    async with _WS_SEMAPHORE:
        return await _generate_outreach_message_inner(
            agent_id=agent_id,
            chat_key=chat_key,
            language=language,
            link_url=link_url,
            promo_code=promo_code,
        )


async def _generate_outreach_message_inner(
    agent_id: str,
    chat_key: str,
    language: str,
    link_url: str,
    promo_code: Optional[str] = None,
) -> str:
    session = _get_session(f"outreach:{chat_key}")

    # Warm-up turn: many agents send default "first message" greeting on a fresh WS session.
    # We consume it first, then request the actual outreach copy.
    # (No dynamic variables on warmup — we just discard the greeting.)
    try:
        await session.ask(
            agent_id=agent_id,
            context_text=WHATSAPP_CONTEXT,
            prior_turns=[],
            last_user_text="Hi",
            language=None,
            dynamic_variables=None,  # Warmup doesn't need variables
        )
    except Exception:
        # If warm-up fails, still attempt the real generation turn.
        pass

    def _normalize_result(text: str) -> str:
        return _ensure_required_outreach_fields(
            text,
            language=language,
            link_url=link_url,
            promo_code=promo_code,
        )

    def _passes_language_guard(text: str) -> bool:
        import re as _re
        # Strip URLs before checking — they can contain brand words (e.g. "pampas" in the link)
        # that would give false-positive matches for the wrong language.
        text_no_urls = _re.sub(r"https?://\S+", "", text)
        lower = text_no_urls.lower()

        if language == "pt-PT":
            banned = ("ars", "vos", "pampas", "olivia", "¿", "¡", "oro casino argentina")
            # Require at least 2 of these Portuguese indicators (not just 1)
            required = ("olá", "contigo", "teu", "rodadas", "boa sorte", "grátis", "código")
            hits = sum(1 for t in required if t in lower)
            return (not any(token in lower for token in banned)) and hits >= 2

        if language == "es-AR":
            banned = ("oro casino", "camila", "teu", "contigo", "rodadas grátis", "olá", "boa sorte")
            # Require at least 2 Spanish indicators (without relying on brand name in URL)
            required = ("hola", "vos", "suerte", "bono", "deposito", "activar", "ars")
            hits = sum(1 for t in required if t in lower)
            return (not any(token in lower for token in banned)) and hits >= 2

        return True

    best = ""
    try:
        for attempt in range(1, 5):
            dynamic_variables = {
                "language": language,
                "link": link_url,
                "promo": promo_code or "",
                "variant_id": f"v{random.randint(1000, 9999)}",
                "anti_spam_seed": f"{int(time.time() * 1000)}-{random.randint(10000, 99999)}",
            }
            opener = random.choice([
                "start with a question to the lead",
                "start with an emoji, then the offer",
                "start with the bonus amount first",
                "start with urgency (limited time)",
                "start with the activation instruction",
                "start with a compliment then offer",
                "start with curiosity hook, no greeting",
                "start with the casino name and a bold claim",
            ])
            tone = random.choice([
                "casual and friendly", "energetic and short",
                "formal but warm", "playful with emojis",
            ])
            instruction = (
                f"Write a UNIQUE WhatsApp outreach message. "
                f"Seed={dynamic_variables['anti_spam_seed']} — your reply MUST differ from all previous ones. "
                f"Opening style: {opener}. Tone: {tone}. "
                f"Language={language}. Include the resolved final link={{link}} and promo={{promo}} naturally. "
                f"Do not output placeholders like {{link}} or {{promo}}. "
                f"Max 3 sentences. Return ONLY the message text."
            )
            reply = await session.ask(
                agent_id=agent_id,
                context_text=WHATSAPP_CONTEXT,
                prior_turns=[],
                last_user_text=instruction,
                language=None,
                dynamic_variables=dynamic_variables,
            )
            result = _normalize_result(reply)
            if not result:
                logger.warning(f"Outreach attempt {attempt}: empty reply from agent")
                continue
            missing_fields = _missing_required_fields(
                result,
                link_url=link_url,
                promo_code=promo_code,
            )
            if missing_fields:
                logger.warning(
                    "Outreach attempt %s missing required fields=%s after normalization: %s",
                    attempt,
                    ",".join(missing_fields),
                    result[:160],
                )
            best = result
            if _passes_language_guard(result):
                logger.info(
                    f"Outreach generated [{language}] attempt={attempt} "
                    f"({len(result)} chars): {result}"
                )
                return result
            
            logger.warning(
                f"Outreach attempt {attempt} rejected by language guard "
                f"(expected {language}): {result[:120]}..."
            )
    finally:
        # Outreach sessions are single-use — close the WebSocket immediately so we
        # don't leak one open connection per lead in bulk sends.
        outreach_key = f"outreach:{chat_key}"
        _SESSIONS.pop(outreach_key, None)
        await session.reset()

    # Hard fallback if the model keeps violating language/casino constraints.
    return _ensure_required_outreach_fields(
        _fallback_outreach(language=language, link_url=link_url, promo_code=promo_code),
        language=language,
        link_url=link_url,
        promo_code=promo_code,
    )


async def _download_audio(
    direct_url: str,
    instance_id: Optional[str] = None,
    api_token: Optional[str] = None,
    message_id: Optional[str] = None,
) -> Optional[bytes]:
    """Download audio bytes. Tries direct URL first, then Green API download endpoint."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(direct_url)
            if r.status_code == 200 and r.content:
                logger.debug(f"Audio downloaded via direct URL ({len(r.content)} bytes)")
                return r.content
            else:
                logger.warning(f"Direct URL returned {r.status_code}, trying Green API endpoint")
        except Exception as e:
            logger.warning(f"Direct URL download failed: {e}, trying Green API endpoint")

        if instance_id and api_token and message_id:
            try:
                from app.services.green_api import build_url
                r = await client.post(
                    build_url(instance_id, api_token, "downloadFile"),
                    json={"idMessage": message_id},
                )
                if r.status_code == 200:
                    import base64
                    file_b64 = r.json().get("body", "")
                    if file_b64:
                        audio_bytes = base64.b64decode(file_b64)
                        logger.debug(f"Audio downloaded via Green API ({len(audio_bytes)} bytes)")
                        return audio_bytes
            except Exception as e:
                logger.error(f"Green API downloadFile also failed: {e}")

    return None


async def transcribe_audio(
    audio_url: str,
    instance_id: Optional[str] = None,
    api_token: Optional[str] = None,
    message_id: Optional[str] = None,
) -> Optional[str]:
    """Download audio and transcribe via ElevenLabs Scribe STT."""
    try:
        audio_bytes = await _download_audio(audio_url, instance_id, api_token, message_id)
        if not audio_bytes:
            logger.error("Could not download audio from any source")
            return None

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{ELEVENLABS_BASE}/speech-to-text",
                headers=_el_headers(),
                files={"file": ("audio.ogg", audio_bytes, "audio/ogg")},
                data={"model_id": "scribe_v1"},
            )
            logger.debug(f"ElevenLabs STT response: {resp.status_code} {resp.text[:200]}")
            resp.raise_for_status()
            text = resp.json().get("text", "").strip()

        if text:
            logger.info(f"Transcribed audio ({len(audio_bytes)} bytes): {text[:100]}")
            return text
        else:
            logger.warning("ElevenLabs STT returned empty text")
            return None

    except Exception as e:
        logger.error(f"Audio transcription failed: {e}")
        return None
