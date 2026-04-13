"""
Reply rate + block rate monitoring for Telegram anti-spam.

Mirrors app/services/reply_monitor.py but uses TelegramMessage / TelegramInstance.
Thresholds are slightly relaxed vs WhatsApp since Telegram's spam detection is
less aggressive than WhatsApp's Quality Rating system.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import Blacklist, Conversation, TelegramInstance, TelegramMessage

logger = logging.getLogger(__name__)

TG_REPLY_RATE_WARNING = 0.20   # below 20% → WARNING
TG_REPLY_RATE_DANGER  = 0.08   # below  8% → excluded from pool
TG_REPLY_RATE_LOOKBACK_DAYS = 7

TG_BLOCK_RATE_WARNING = 0.03   # 3%
TG_BLOCK_RATE_DANGER  = 0.07   # 7%


async def get_tg_reply_rate(
    db: AsyncSession,
    phone_number: str,
    days: int = TG_REPLY_RATE_LOOKBACK_DAYS,
) -> Optional[float]:
    """Reply rate for one TelegramInstance over the last `days` days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    InboundMsg = aliased(TelegramMessage, name="tg_inbound_msg")

    total_res = await db.execute(
        select(func.count(distinct(TelegramMessage.conversation_id)))
        .join(TelegramInstance, TelegramMessage.instance_id == TelegramInstance.id)
        .where(
            TelegramInstance.phone_number == phone_number,
            TelegramMessage.direction == "outbound",
            TelegramMessage.created_at >= cutoff,
        )
    )
    total: int = total_res.scalar_one() or 0
    if total == 0:
        return None

    outbound_convs_sq = (
        select(distinct(TelegramMessage.conversation_id))
        .join(TelegramInstance, TelegramMessage.instance_id == TelegramInstance.id)
        .where(
            TelegramInstance.phone_number == phone_number,
            TelegramMessage.direction == "outbound",
            TelegramMessage.created_at >= cutoff,
        )
        .scalar_subquery()
    )

    replied_res = await db.execute(
        select(func.count(distinct(InboundMsg.conversation_id)))
        .where(
            InboundMsg.direction == "inbound",
            InboundMsg.conversation_id.in_(outbound_convs_sq),
        )
    )
    replied: int = replied_res.scalar_one() or 0
    rate = replied / total
    logger.debug("[TGReplyMonitor] phone=%s days=%d sent=%d replied=%d rate=%.3f",
                 phone_number, days, total, replied, rate)
    return rate


async def get_tg_block_rate(
    db: AsyncSession,
    phone_number: str,
    days: int = TG_REPLY_RATE_LOOKBACK_DAYS,
) -> Optional[float]:
    """Block rate for one TelegramInstance over the last `days` days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    total_res = await db.execute(
        select(func.count(distinct(TelegramMessage.conversation_id)))
        .join(TelegramInstance, TelegramMessage.instance_id == TelegramInstance.id)
        .where(
            TelegramInstance.phone_number == phone_number,
            TelegramMessage.direction == "outbound",
            TelegramMessage.created_at >= cutoff,
        )
    )
    total: int = total_res.scalar_one() or 0
    if total == 0:
        return None

    outbound_convs_sq = (
        select(distinct(TelegramMessage.conversation_id))
        .join(TelegramInstance, TelegramMessage.instance_id == TelegramInstance.id)
        .where(
            TelegramInstance.phone_number == phone_number,
            TelegramMessage.direction == "outbound",
            TelegramMessage.created_at >= cutoff,
        )
        .scalar_subquery()
    )

    blocked_res = await db.execute(
        select(func.count(distinct(Conversation.id)))
        .join(Blacklist, Conversation.phone == Blacklist.phone)
        .where(Conversation.id.in_(outbound_convs_sq))
    )
    blocked: int = blocked_res.scalar_one() or 0
    rate = blocked / total
    logger.debug("[TGBlockMonitor] phone=%s days=%d sent=%d blocked=%d rate=%.4f",
                 phone_number, days, total, blocked, rate)
    return rate


def classify_tg_reply_rate(rate: Optional[float]) -> str:
    if rate is None:
        return "no_data"
    if rate < TG_REPLY_RATE_DANGER:
        return "danger"
    if rate < TG_REPLY_RATE_WARNING:
        return "warning"
    return "ok"


def classify_tg_block_rate(rate: Optional[float]) -> str:
    if rate is None:
        return "no_data"
    if rate > TG_BLOCK_RATE_DANGER:
        return "danger"
    if rate > TG_BLOCK_RATE_WARNING:
        return "warning"
    return "ok"
