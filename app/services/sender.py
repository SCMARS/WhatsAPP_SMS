import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, WhatsAppInstance, WhatsAppMessage
from app.services import pool as instance_pool
from app.services.rate_limiter import batch_pause, personalize_message, reply_pause, wait_before_send

logger = logging.getLogger(__name__)


def _format_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    if not digits.endswith("@c.us"):
        return f"{digits}@c.us"
    return digits


async def read_chat(db: AsyncSession, conversation: Conversation) -> bool:
    """Mark a chat as read in Green API. This helps with E2EE sync."""
    instance = await instance_pool.get_best_instance(db, is_reservation=False)
    if not instance:
        return False

    chat_id = _format_phone(conversation.phone)
    url = (
        f"https://7107.api.greenapi.com"
        f"/waInstance{instance.instance_id}"
        f"/readChat/{instance.api_token}"
    )
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={"chatId": chat_id})
            return resp.status_code == 200
    except Exception as e:
        logger.warning(f"Failed to readChat for {chat_id}: {e}")
        return False


async def send_message(
    db: AsyncSession,
    conversation: Conversation,
    text: str,
    lead_name: Optional[str] = None,
    batch_index: int = 0,
    is_reply: bool = False,
    instance: Optional[WhatsAppInstance] = None,
) -> Optional[WhatsAppMessage]:
    if not instance:
        instance = await instance_pool.get_best_instance(db)
    
    if not instance:
        logger.error("No available WhatsApp instances in pool")
        return None

    # Personalize and apply batch pause
    personalized = personalize_message(text, lead_name)
    if is_reply:
        # Short human-like typing delay for AI replies (2–5s)
        await reply_pause()
    else:
        # Full anti-spam delay for bulk outreach
        await batch_pause(batch_index)
        await wait_before_send(instance)

    chat_id = _format_phone(conversation.phone)
    provider_message_id: Optional[str] = None
    error_text: Optional[str] = None
    status = "failed"

    try:
        provider_message_id, error_text, status = await _do_send(
            instance, chat_id, personalized
        )
    except Exception as e:
        error_text = str(e)
        err_lower = error_text.lower()
        if "banned" in err_lower or "blocked" in err_lower:
            logger.warning(f"Instance {instance.instance_id} appears banned: {e}")
            await instance_pool.mark_banned(db, instance.instance_id)
        else:
            logger.error(f"Send failed for {conversation.phone}: {e}")
    else:
        # Some Green API errors mean the instance is unusable (deleted/invalid).
        # Mark it banned so the pool stops selecting it.
        if status == "failed" and (error_text or "").lower().find("instance is deleted") != -1:
            logger.warning(f"Instance {instance.instance_id} appears deleted/invalid, disabling it")
            await instance_pool.mark_banned(db, instance.instance_id)

    if status in ("sent", "queued"):
        await instance_pool.record_send(instance.instance_id)

    msg = WhatsAppMessage(
        conversation_id=conversation.id,
        instance_id=instance.id,
        direction="outbound",
        body=personalized,
        provider_message_id=provider_message_id,
        status=status,
        error=error_text,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)

    logger.info(f"Message to {conversation.phone} status={status} id={msg.id}")
    return msg


def calc_typing_time(message: str) -> int:
    import random
    chars = len(message)
    ms = (chars / 3) * 1000  # 3 chars per sec
    ms = ms * random.uniform(0.8, 1.2)  # +- 20% jitter
    return int(max(2000, min(ms, 8000)))  # Cap between 2 and 8 secs


async def _do_send(
    instance: WhatsAppInstance,
    chat_id: str,
    text: str,
) -> tuple[Optional[str], Optional[str], str]:
    """Send typing indicator, wait, then send message via Green API. Returns (provider_id, error, status)."""
    import httpx

    import asyncio
    
    # Send "typing..." indicator first
    typing_ms = calc_typing_time(text)
    typing_url = (
        f"https://7107.api.greenapi.com"
        f"/waInstance{instance.instance_id}"
        f"/sendTyping/{instance.api_token}"
    )
    typing_payload = {
        "chatId": chat_id,
        "typingTime": typing_ms
    }

    url = (
        f"https://7107.api.greenapi.com"
        f"/waInstance{instance.instance_id}"
        f"/sendMessage/{instance.api_token}"
    )
    payload = {
        "chatId": chat_id,
        "message": text,
        "linkPreview": False,  # Simpler payload to avoid E2EE sync delays
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            # 1. Fire typing indicator
            await client.post(typing_url, json=typing_payload)
            # 2. Emulate the typing duration waiting time block
            await asyncio.sleep(typing_ms / 1000.0)
        except Exception as e:
            logger.warning(f"Failed to send typing indicator: {e}")
            
        # 3. Actually send the message
        resp = await client.post(url, json=payload)

    if resp.status_code in (403, 429):
        raise RuntimeError(f"HTTP {resp.status_code} from Green API — likely banned/rate-limited")

    data = {}
    try:
        data = resp.json()
    except Exception:
        pass

    if resp.status_code != 200:
        # Green API often returns structured error; include raw text for debugging.
        raw = ""
        try:
            raw = resp.text
        except Exception:
            raw = ""
        error = data.get("message") or data.get("error") or (raw.strip() if raw else f"HTTP {resp.status_code}")
        logger.warning(
            "Green API sendMessage failed: status=%s instance_id=%s chat_id=%s body=%s",
            resp.status_code,
            instance.instance_id,
            chat_id,
            (raw[:500] if raw else data),
        )
        return None, error, "failed"

    provider_id = data.get("idMessage")
    if provider_id:
        logger.info(f"Green API: message sent, provider_id={provider_id}")
    return provider_id, None, "sent"


async def send_initial_message(
    db: AsyncSession,
    conversation: Conversation,
    initial_text: str | list[str],
    batch_index: int = 0,
) -> Optional[WhatsAppMessage]:
    messages = [initial_text] if isinstance(initial_text, str) else initial_text
    last_msg = None
    selected_instance = None

    for i, text in enumerate(messages):
        # 1st message: Selects and reserves instance (4 min cooldown)
        # Subsequent: Reuses same instance with short typing delay
        is_seq = i > 0
        
        msg = await send_message(
            db=db,
            conversation=conversation,
            text=text,
            lead_name=conversation.lead_name,
            batch_index=batch_index,
            is_reply=is_seq,
            instance=selected_instance,
        )
        if i == 0:
            last_msg = msg
            if msg:
                # Get the instance object back to reuse its ID/ID
                from sqlalchemy import select
                res = await db.execute(select(WhatsAppInstance).where(WhatsAppInstance.id == msg.instance_id))
                selected_instance = res.scalar_one_or_none()
        
        # If there are more messages, wait a bit before the next one
        if i < len(messages) - 1:
            await reply_pause(min_sec=3.0, max_sec=6.0)
            
    return last_msg
