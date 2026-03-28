import logging
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Blacklist, Conversation

logger = logging.getLogger(__name__)

STOP_KEYWORDS = {
    "stop", "стоп", "отписаться", "отписать", "не пишите", "не беспокоить",
    "хватит", "unsubscribe", "quit", "cancel", "отмена", "удалите", "уберите",
}


def is_stop_message(text: str) -> bool:
    normalized = text.lower().strip()
    for keyword in STOP_KEYWORDS:
        if keyword in normalized:
            return True
    return False


async def add_to_blacklist(
    db: AsyncSession,
    phone: str,
    reason: Optional[str] = None,
) -> None:
    # Check if already in blacklist
    result = await db.execute(select(Blacklist).where(Blacklist.phone == phone))
    existing = result.scalar_one_or_none()
    if not existing:
        db.add(Blacklist(phone=phone, reason=reason))

    # Close all active conversations for this phone
    await db.execute(
        update(Conversation)
        .where(Conversation.phone == phone, Conversation.status == "active")
        .values(status="closed", is_blacklisted=True)
    )
    await db.commit()
    logger.info(f"Phone {phone} added to blacklist. Reason: {reason}")


async def is_blacklisted(db: AsyncSession, phone: str) -> bool:
    result = await db.execute(select(Blacklist).where(Blacklist.phone == phone))
    return result.scalar_one_or_none() is not None
