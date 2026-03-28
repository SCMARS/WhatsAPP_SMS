"""
ElevenLabs ConvAI integration — text-only mode via WebSocket.

The agent must have "Text mode" enabled in ElevenLabs dashboard (Advanced tab).
System prompt is configured on the agent in ElevenLabs — no local override needed.

Protocol:
1. Connect → receive conversation_initiation_metadata
2. Send conversation_initiation_client_data with text_only=true
3. Immediately send user_message
4. Collect agent_chat_response_part deltas → agent_response (final text)
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
    "\n\n[CONTEXT: This is a WhatsApp text conversation. "
    "Keep replies short — max 3-4 sentences. Text only, no markdown.]"
)


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


async def transcribe_audio(audio_url: str) -> Optional[str]:
    """
    Download audio from Green API and transcribe via ElevenLabs Scribe STT.
    Returns transcribed text or None on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # 1. Download audio file
            audio_resp = await client.get(audio_url)
            audio_resp.raise_for_status()
            audio_bytes = audio_resp.content
            content_type = audio_resp.headers.get("content-type", "audio/ogg")

        # 2. Send to ElevenLabs Scribe STT
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{ELEVENLABS_BASE}/speech-to-text",
                headers=_el_headers(),
                files={"file": ("audio.ogg", audio_bytes, content_type)},
                data={"model_id": "scribe_v1"},
            )
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
) -> str:
    """
    Generate a text reply via ElevenLabs ConvAI WebSocket (text-only mode).
    Sends conversation history as contextual_update before the user message.
    """
    user_messages = [m for m in history if m.get("role") == "user"]
    if not user_messages:
        logger.warning("No user messages in history")
        return ""

    last_user_text = user_messages[-1]["content"]

    # Add lead name and WhatsApp context hint as contextual update
    context_parts = []
    if lead_name:
        context_parts.append(f"Customer name: {lead_name}.")
    context_parts.append(
        "This is a WhatsApp text chat. Keep your reply short — max 3-4 sentences. Plain text only."
    )
    context_text = " ".join(context_parts)

    # Prior conversation turns (excluding last user message)
    prior_turns = history[:-1]

    signed_url = await _get_signed_url(agent_id)
    reply = ""

    try:
        async with websockets.connect(
            signed_url,
            ssl=_ssl_ctx(),
            open_timeout=15,
            close_timeout=10,
        ) as ws:

            # 1. Wait for conversation_initiation_metadata
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "conversation_initiation_metadata":
                    break

            # 2. Enable text-only mode
            await ws.send(json.dumps({
                "type": "conversation_initiation_client_data",
                "conversation_config_override": {
                    "conversation": {"text_only": True},
                },
            }))

            # 3. Inject prior conversation history as contextual_update (no agent response)
            if prior_turns:
                history_text = "\n".join(
                    f"{'User' if m['role'] == 'user' else 'Agent'}: {m['content']}"
                    for m in prior_turns
                )
                await ws.send(json.dumps({
                    "type": "contextual_update",
                    "text": f"Previous conversation:\n{history_text}",
                }))

            # 4. Send WhatsApp context hint
            await ws.send(json.dumps({
                "type": "contextual_update",
                "text": context_text,
            }))

            # 5. Send the actual user message
            await ws.send(json.dumps({
                "type": "user_message",
                "text": last_user_text,
            }))
            logger.debug(f"user_message sent: {last_user_text[:60]}")

            # 6. Collect agent_response
            async def _collect():
                nonlocal reply
                async for raw in ws:
                    msg = json.loads(raw)
                    t = msg.get("type", "")

                    if t == "ping":
                        eid = msg.get("ping_event", {}).get("event_id")
                        await ws.send(json.dumps({"type": "pong", "event_id": eid}))

                    elif t == "agent_response":
                        text = msg.get("agent_response_event", {}).get("agent_response", "")
                        reply = text.strip()
                        await ws.close()
                        return

                    elif t in ("agent_chat_response_part", "internal_tentative_agent_response"):
                        pass  # streaming parts — wait for final agent_response

            await asyncio.wait_for(_collect(), timeout=45)

    except asyncio.TimeoutError:
        logger.warning("ElevenLabs WebSocket timeout after 45s")
    except websockets.exceptions.ConnectionClosed as e:
        logger.debug(f"WebSocket closed: {e}")
    except Exception as e:
        logger.error(f"ElevenLabs WebSocket error: {e}")
        raise

    logger.info(f"ElevenLabs reply ({len(reply)} chars): {reply[:120]}")
    return reply
