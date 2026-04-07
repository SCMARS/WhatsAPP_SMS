"""
ElevenLabs integration — ConvAI WebSocket (text-only) + STT (Scribe).

Conversation continuity:
  - On first message: connect without conversation_id, save returned id to DB.
  - On subsequent messages: append conversation_id as query param → ElevenLabs
    resumes the SAME conversation (one chat per WhatsApp contact).
"""
import asyncio
import json
import logging
import ssl
from typing import Optional

import certifi
import httpx
import websockets
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings

logger = logging.getLogger(__name__)

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"

WHATSAPP_CONTEXT = (
    "This is a WhatsApp text chat. Keep your reply short — max 3-4 sentences. "
    "Plain text only, no markdown."
)

class _ChatSession:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.last_used = asyncio.get_event_loop().time()

    async def _ensure_connected(self, agent_id: str) -> websockets.WebSocketClientProtocol:
        if self.ws is not None:
            return self.ws

        signed_url = await _get_signed_url(agent_id)
        ws = await websockets.connect(
            signed_url,
            ssl=_ssl_ctx(),
            open_timeout=15,
            close_timeout=10,
            # Disable client-side keepalive timeout which caused false 1011 disconnects.
            ping_interval=None,
            ping_timeout=None,
        )

        # Wait for initiation metadata
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "conversation_initiation_metadata":
                break

        # Enable text-only mode once per socket
        await ws.send(json.dumps({
            "type": "conversation_initiation_client_data",
            "conversation_config_override": {
                "conversation": {"text_only": True},
            },
        }))

        self.ws = ws
        return ws

    async def ask(
        self,
        agent_id: str,
        context_text: str,
        prior_turns: list[dict],
        last_user_text: str,
    ) -> str:
        async with self.lock:
            self.last_used = asyncio.get_event_loop().time()
            reply = ""

            async def _send_turn(ws: websockets.WebSocketClientProtocol) -> None:
                # Inject history + context each turn (cheap, keeps agent grounded)
                if prior_turns:
                    history_text = "\n".join(
                        f"{'User' if m['role'] == 'user' else 'Agent'}: {m['content']}"
                        for m in prior_turns
                    )
                    await ws.send(json.dumps({
                        "type": "contextual_update",
                        "text": f"Previous conversation:\n{history_text}",
                    }))

                await ws.send(json.dumps({
                    "type": "contextual_update",
                    "text": context_text,
                }))

                await ws.send(json.dumps({
                    "type": "user_message",
                    "text": last_user_text,
                }))

            async def _collect(ws: websockets.WebSocketClientProtocol) -> None:
                nonlocal reply
                async for raw in ws:
                    msg = json.loads(raw)
                    t = msg.get("type", "")

                    if t == "ping":
                        eid = msg.get("ping_event", {}).get("event_id")
                        await ws.send(json.dumps({"type": "pong", "event_id": eid}))
                        continue

                    if t == "agent_response":
                        text = msg.get("agent_response_event", {}).get("agent_response", "")
                        reply = (text or "").strip()
                        return

            async def _run_turn(ws: websockets.WebSocketClientProtocol) -> None:
                await _send_turn(ws)
                await asyncio.wait_for(_collect(ws), timeout=45)

            ws = await self._ensure_connected(agent_id)
            try:
                await _run_turn(ws)
            except Exception:
                # Socket may be stale; drop it so next call reconnects
                try:
                    await ws.close()
                except Exception:
                    pass
                self.ws = None
                # Retry once with a fresh socket
                ws = await self._ensure_connected(agent_id)
                reply = ""
                await _run_turn(ws)

            return reply

    async def reset(self) -> None:
        async with self.lock:
            if self.ws is not None:
                try:
                    await self.ws.close()
                except Exception:
                    pass
            self.ws = None


_SESSIONS: dict[str, _ChatSession] = {}


def _get_session(chat_key: str) -> _ChatSession:
    s = _SESSIONS.get(chat_key)
    if s is None:
        s = _ChatSession()
        _SESSIONS[chat_key] = s
    return s


def _el_headers() -> dict:
    return {"xi-api-key": settings.ELEVENLABS_API_KEY}


def _ssl_ctx() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
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


async def _get_signed_url(agent_id: str) -> str:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{ELEVENLABS_BASE}/convai/conversation/get_signed_url",
            headers=_el_headers(),
            params={"agent_id": agent_id},
        )
        resp.raise_for_status()
        return resp.json()["signed_url"]


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


async def generate_text_reply(
    agent_id: str,
    system_prompt: str,
    history: list[dict],
    lead_name: Optional[str] = None,
    chat_key: Optional[str] = None,
) -> str:
    """
    Generate a text reply via ElevenLabs ConvAI WebSocket (text-only mode).
    Sends full conversation history as contextual_update before the user message.
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
        )
    except asyncio.TimeoutError:
        logger.warning("ElevenLabs WebSocket timeout after 45s")
        return ""
    except Exception as e:
        err = str(e).lower()
        # One hard retry on websocket-level failures.
        if (
            "1011" in err
            or "1002" in err
            or "keepalive ping timeout" in err
            or "connectionclosed" in err
            or "failed to generate a response" in err
        ):
            logger.warning(f"ElevenLabs transient socket error, retrying once: {e}")
            await session.reset()
            try:
                reply = await session.ask(
                    agent_id=agent_id,
                    context_text=context_text,
                    prior_turns=prior_turns,
                    last_user_text=last_user_text,
                )
            except asyncio.TimeoutError:
                logger.warning("ElevenLabs retry timed out after 45s")
                return ""
            except Exception as retry_err:
                logger.error(f"ElevenLabs retry failed: {retry_err}")
                return ""
        else:
            logger.error(f"ElevenLabs generate_text_reply failed: {e}")
        return ""

    logger.info(f"ElevenLabs reply ({len(reply)} chars): {reply[:120]}")
    return reply
