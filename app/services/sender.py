import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, WhatsAppInstance, WhatsAppMessage
from app.services import pool as instance_pool
from app.services.rate_limiter import batch_pause, personalize_message, wait_before_send

logger = logging.getLogger(__name__)


def _format_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    return f"{digits}@c.us"


async def send_message(
    db: AsyncSession,
    conversation: Conversation,
    text: str,
    lead_name: Optional[str] = None,
    batch_index: int = 0,
) -> Optional[WhatsAppMessage]:
    instance = await instance_pool.get_best_instance(db)
    if not instance:
        logger.error("No available WhatsApp instances in pool")
        return None

    # Personalize and apply batch pause
    personalized = personalize_message(text, lead_name)
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


async def _do_send(
    instance: WhatsAppInstance,
    chat_id: str,
    text: str,
) -> tuple[Optional[str], Optional[str], str]:
    """Send message via Green API using direct httpx call. Returns (provider_id, error, status)."""
    import httpx

    url = (
        f"https://7107.api.greenapi.com"
        f"/waInstance{instance.instance_id}"
        f"/sendMessage/{instance.api_token}"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json={"chatId": chat_id, "message": text})

    if resp.status_code in (403, 429):
        raise RuntimeError(f"HTTP {resp.status_code} from Green API — likely banned/rate-limited")

    data = {}
    try:
        data = resp.json()
    except Exception:
        pass

    if resp.status_code != 200:
        error = data.get("message", f"HTTP {resp.status_code}")
        return None, error, "failed"

    provider_id = data.get("idMessage")
    return provider_id, None, "sent"


async def send_initial_message(
    db: AsyncSession,
    conversation: Conversation,
    initial_text: str,
    batch_index: int = 0,
) -> Optional[WhatsAppMessage]:
    return await send_message(
        db=db,
        conversation=conversation,
        text=initial_text,
        lead_name=conversation.lead_name,
        batch_index=batch_index,
    )
