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
import ssl
from typing import Optional

import certifi
import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from app.config import settings

logger = logging.getLogger(__name__)

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"

WHATSAPP_CONTEXT = (
    "This is a WhatsApp text chat. Keep your reply short — max 3-4 sentences. "
    "Plain text only, no markdown."
)


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

    # 2. Enable text-only mode
    config_override: dict = {"conversation": {"text_only": True}}

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
                logger.warning("ElevenLabs _collect timed out after 45s")
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
    result = template
    result = result.replace("{{link}}",  link_url   or "")
    result = result.replace("{{promo}}", promo_code or "")
    result = result.replace("{link}",    link_url   or "")
    result = result.replace("{promo}",   promo_code or "")

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
    dynamic_variables = {
        "language": language,
        "link": link_url,
        "promo": promo_code or "",
        "variant_id": f"v{random.randint(1000, 9999)}",
    }

    session = _get_session(f"outreach:{chat_key}")

    # Warm-up turn: many agents send default "first message" greeting on a fresh WS session.
    # We consume it first, then request the actual outreach copy.
    try:
        await session.ask(
            agent_id=agent_id,
            context_text=WHATSAPP_CONTEXT,
            prior_turns=[],
            last_user_text="Hi",
            language=None,
            dynamic_variables=dynamic_variables,
        )
    except Exception:
        # If warm-up fails, still attempt the real generation turn.
        pass

    style_hint = random.choice([
        "start with greeting then offer",
        "start with offer then greeting",
        "start with activation instruction then greeting",
    ])
    instruction = (
        "Generate ONE WhatsApp outreach message now. "
        "Use variables language={language}, link={link}, promo={promo}. "
        f"Style hint: {style_hint}. "
        f"Variant id: {dynamic_variables['variant_id']}. "
        "Do not reuse your previous opening phrase. "
        "Return only the final message text, no labels."
    )
    reply = await session.ask(
        agent_id=agent_id,
        context_text=WHATSAPP_CONTEXT,
        prior_turns=[],
        last_user_text=instruction,
        language=None,
        dynamic_variables=dynamic_variables,
    )
    result = (reply or "").strip()
    if not result:
        return ""

    # Safety net: if the model outputs literal placeholders, replace them locally.
    result = result.replace("{{link}}", link_url or "")
    result = result.replace("{{promo}}", promo_code or "")
    result = result.replace("{{language}}", language or "")
    result = result.replace("{link}", link_url or "")
    result = result.replace("{promo}", promo_code or "")
    result = result.replace("{language}", language or "")
    return result.strip()


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
                green_url = (
                    f"https://7107.api.greenapi.com"
                    f"/waInstance{instance_id}"
                    f"/downloadFile/{api_token}"
                )
                r = await client.post(green_url, json={"idMessage": message_id})
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
