"""
Reply rate + block rate monitoring for WhatsApp anti-spam compliance.

WhatsApp spam detection signals (from Green API docs + community data):
  Reply rate  — primary signal
    > 30%  — safe
    15–30% — warning, monitor closely
    < 15%  — high ban risk

  Block rate  — critical signal (users blocked/reported the number)
    < 1%   — excellent
    1–2%   — warning
    > 2%   — quality rating drop, imminent restriction
    > 5%   — high ban risk

Inbound messages have instance_id = NULL (see handler.py).
We link replies to an instance via conversation:
  outbound message → conversation_id → inbound message on that conversation.

Block rate is computed as:
  conversations where phone ended up in blacklist
  ──────────────────────────────────────────────
  total conversations this instance sent outbound to
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import Blacklist, Conversation, WhatsAppInstance, WhatsAppMessage

logger = logging.getLogger(__name__)

REPLY_RATE_WARNING = 0.30   # below → WARNING log
REPLY_RATE_DANGER  = 0.15   # below → ERROR log, excluded from pool
REPLY_RATE_LOOKBACK_DAYS = 7

BLOCK_RATE_WARNING = 0.02   # 2%  — quality rating at risk
BLOCK_RATE_DANGER  = 0.05   # 5%  — high ban risk


async def get_reply_rate(
    db: AsyncSession,
    instance_id: str,
    days: int = REPLY_RATE_LOOKBACK_DAYS,
) -> Optional[float]:
    """
    Return reply rate (0.0–1.0) for one instance over the last `days` days.

    Reply rate = conversations that received ≥1 inbound reply
                 ─────────────────────────────────────────────
                 conversations where this instance sent ≥1 outbound message

    Returns None if the instance sent nothing in the window (new / idle).
    Returns 0.0  if it sent messages but nobody replied.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Alias for the inbound-side of the query to avoid table name collision
    InboundMsg = aliased(WhatsAppMessage, name="inbound_msg")

    # Subquery: distinct conversation_ids where THIS instance sent outbound messages
    outbound_convs_sq = (
        select(distinct(WhatsAppMessage.conversation_id))
        .join(WhatsAppInstance, WhatsAppMessage.instance_id == WhatsAppInstance.id)
        .where(
            WhatsAppInstance.instance_id == instance_id,
            WhatsAppMessage.direction == "outbound",
            WhatsAppMessage.created_at >= cutoff,
        )
        .scalar_subquery()
    )

    # Total: how many distinct conversations did this instance touch?
    total_res = await db.execute(
        select(func.count(distinct(WhatsAppMessage.conversation_id)))
        .join(WhatsAppInstance, WhatsAppMessage.instance_id == WhatsAppInstance.id)
        .where(
            WhatsAppInstance.instance_id == instance_id,
            WhatsAppMessage.direction == "outbound",
            WhatsAppMessage.created_at >= cutoff,
        )
    )
    total: int = total_res.scalar_one() or 0

    if total == 0:
        return None  # no data — instance is new or idle

    # Replied: of those conversations, how many got ≥1 inbound message?
    replied_res = await db.execute(
        select(func.count(distinct(InboundMsg.conversation_id)))
        .where(
            InboundMsg.direction == "inbound",
            InboundMsg.conversation_id.in_(outbound_convs_sq),
        )
    )
    replied: int = replied_res.scalar_one() or 0

    rate = replied / total
    logger.debug(
        "[ReplyMonitor] instance=%s days=%d sent_to=%d replied=%d rate=%.3f",
        instance_id, days, total, replied, rate,
    )
    return rate


async def get_all_reply_rates(
    db: AsyncSession,
    days: int = REPLY_RATE_LOOKBACK_DAYS,
) -> dict[str, Optional[float]]:
    """
    Return {instance_id: reply_rate} for every instance that sent messages
    in the window. Instances with no traffic are absent from the result.
    Uses two bulk queries instead of N per-instance queries.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    InboundMsg = aliased(WhatsAppMessage, name="inbound_msg_bulk")

    # 1. Per-instance: distinct conversations with ≥1 outbound in window
    outbound_q = await db.execute(
        select(
            WhatsAppInstance.instance_id,
            func.count(distinct(WhatsAppMessage.conversation_id)).label("total"),
        )
        .join(WhatsAppMessage, WhatsAppMessage.instance_id == WhatsAppInstance.id)
        .where(
            WhatsAppMessage.direction == "outbound",
            WhatsAppMessage.created_at >= cutoff,
        )
        .group_by(WhatsAppInstance.instance_id)
    )
    totals: dict[str, int] = {row.instance_id: row.total for row in outbound_q.all()}

    if not totals:
        return {}

    # Subquery: ALL conversation_ids that have any inbound message
    replied_convs_sq = (
        select(distinct(InboundMsg.conversation_id))
        .where(InboundMsg.direction == "inbound")
        .scalar_subquery()
    )

    # 2. Per-instance: conversations (outbound) that also appear in replied_convs
    replied_q = await db.execute(
        select(
            WhatsAppInstance.instance_id,
            func.count(distinct(WhatsAppMessage.conversation_id)).label("replied"),
        )
        .join(WhatsAppMessage, WhatsAppMessage.instance_id == WhatsAppInstance.id)
        .where(
            WhatsAppMessage.direction == "outbound",
            WhatsAppMessage.created_at >= cutoff,
            WhatsAppMessage.conversation_id.in_(replied_convs_sq),
        )
        .group_by(WhatsAppInstance.instance_id)
    )
    replied_map: dict[str, int] = {row.instance_id: row.replied for row in replied_q.all()}

    rates: dict[str, Optional[float]] = {}
    for inst_id, total in totals.items():
        replied = replied_map.get(inst_id, 0)
        rates[inst_id] = replied / total if total > 0 else 0.0

    return rates


def classify_reply_rate(rate: Optional[float]) -> str:
    """Return 'ok' | 'warning' | 'danger' | 'no_data'."""
    if rate is None:
        return "no_data"
    if rate < REPLY_RATE_DANGER:
        return "danger"
    if rate < REPLY_RATE_WARNING:
        return "warning"
    return "ok"


# ---------------------------------------------------------------------------
# Block rate — what fraction of reached conversations ended in a block/report
# ---------------------------------------------------------------------------

async def get_block_rate(
    db: AsyncSession,
    instance_id: str,
    days: int = REPLY_RATE_LOOKBACK_DAYS,
) -> Optional[float]:
    """
    Return block rate (0.0–1.0) for one instance over the last `days` days.

    Block rate = conversations where phone is now in blacklist
                 ─────────────────────────────────────────────
                 total conversations this instance sent to

    Returns None if the instance sent nothing in the window.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Total outbound conversations
    total_res = await db.execute(
        select(func.count(distinct(WhatsAppMessage.conversation_id)))
        .join(WhatsAppInstance, WhatsAppMessage.instance_id == WhatsAppInstance.id)
        .where(
            WhatsAppInstance.instance_id == instance_id,
            WhatsAppMessage.direction == "outbound",
            WhatsAppMessage.created_at >= cutoff,
        )
    )
    total: int = total_res.scalar_one() or 0
    if total == 0:
        return None

    # Subquery: conversation_ids this instance sent to
    outbound_convs_sq = (
        select(distinct(WhatsAppMessage.conversation_id))
        .join(WhatsAppInstance, WhatsAppMessage.instance_id == WhatsAppInstance.id)
        .where(
            WhatsAppInstance.instance_id == instance_id,
            WhatsAppMessage.direction == "outbound",
            WhatsAppMessage.created_at >= cutoff,
        )
        .scalar_subquery()
    )

    # Blocked: conversations whose phone ended up in blacklist
    blocked_res = await db.execute(
        select(func.count(distinct(Conversation.id)))
        .join(Blacklist, Conversation.phone == Blacklist.phone)
        .where(Conversation.id.in_(outbound_convs_sq))
    )
    blocked: int = blocked_res.scalar_one() or 0

    rate = blocked / total
    logger.debug(
        "[BlockMonitor] instance=%s days=%d sent_to=%d blocked=%d rate=%.4f",
        instance_id, days, total, blocked, rate,
    )
    return rate


async def get_all_block_rates(
    db: AsyncSession,
    days: int = REPLY_RATE_LOOKBACK_DAYS,
) -> dict[str, Optional[float]]:
    """
    Return {instance_id: block_rate} for every instance that sent messages.
    Uses bulk queries (not N per-instance queries).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # 1. Per-instance total outbound conversations
    totals_q = await db.execute(
        select(
            WhatsAppInstance.instance_id,
            func.count(distinct(WhatsAppMessage.conversation_id)).label("total"),
        )
        .join(WhatsAppMessage, WhatsAppMessage.instance_id == WhatsAppInstance.id)
        .where(
            WhatsAppMessage.direction == "outbound",
            WhatsAppMessage.created_at >= cutoff,
        )
        .group_by(WhatsAppInstance.instance_id)
    )
    totals: dict[str, int] = {row.instance_id: row.total for row in totals_q.all()}

    if not totals:
        return {}

    # Subquery: all blacklisted phones
    blacklisted_phones_sq = select(distinct(Blacklist.phone)).scalar_subquery()

    # 2. Per-instance: outbound conversations where phone is now blacklisted
    blocked_q = await db.execute(
        select(
            WhatsAppInstance.instance_id,
            func.count(distinct(WhatsAppMessage.conversation_id)).label("blocked"),
        )
        .join(WhatsAppMessage, WhatsAppMessage.instance_id == WhatsAppInstance.id)
        .join(Conversation, Conversation.id == WhatsAppMessage.conversation_id)
        .where(
            WhatsAppMessage.direction == "outbound",
            WhatsAppMessage.created_at >= cutoff,
            Conversation.phone.in_(blacklisted_phones_sq),
        )
        .group_by(WhatsAppInstance.instance_id)
    )
    blocked_map: dict[str, int] = {row.instance_id: row.blocked for row in blocked_q.all()}

    rates: dict[str, Optional[float]] = {}
    for inst_id, total in totals.items():
        blocked = blocked_map.get(inst_id, 0)
        rates[inst_id] = blocked / total if total > 0 else 0.0

    return rates


def classify_block_rate(rate: Optional[float]) -> str:
    """Return 'ok' | 'warning' | 'danger' | 'no_data'."""
    if rate is None:
        return "no_data"
    if rate > BLOCK_RATE_DANGER:
        return "danger"
    if rate > BLOCK_RATE_WARNING:
        return "warning"
    return "ok"
