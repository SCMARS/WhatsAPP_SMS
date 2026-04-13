"""
Telegram instance pool — mirrors app/services/pool.py for TelegramInstance.

Key differences from WhatsApp pool:
  - Keyed by phone_number (not instance_id string)
  - More conservative warmup caps (Telegram is stricter for new accounts)
  - Extra filter: flood_wait_count < 3 (deprioritise flood-prone accounts)
  - No preferred-instance override (Telegram accounts are all equal)
"""

import asyncio
import logging
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TelegramInstance, TelegramMessage
from app.services.telegram.reply_monitor import (
    TG_BLOCK_RATE_DANGER,
    TG_REPLY_RATE_DANGER,
    get_tg_block_rate,
    get_tg_reply_rate,
)

logger = logging.getLogger(__name__)

# In-memory send log: phone_number → list of send datetimes (last 24h)
_send_log: dict[str, list[datetime]] = defaultdict(list)
_lock = asyncio.Lock()

# Cache for rest-day check: phone_number → (utc_date_str, needs_rest)
_rest_cache: dict[str, tuple[str, bool]] = {}

# ---------------------------------------------------------------------------
# Warmup schedule — more conservative than WhatsApp for new Telegram accounts
# Format: (max_age_days, daily_cap, hourly_cap)
# ---------------------------------------------------------------------------
TG_WARMUP_SCHEDULE: list[tuple[int, int, int]] = [
    (7,  20,  3),    # Week 1: 20/day,   3/hour  — very cautious for new account
    (14, 50,  7),    # Week 2: 50/day,   7/hour
    (21, 100, 12),   # Week 3: 100/day, 12/hour
    (30, 150, 18),   # Week 4: 150/day, 18/hour
    # Day 30+: use configured daily_limit / hourly_limit (max ~200/day)
]


# ---------------------------------------------------------------------------
# Warmup helpers
# ---------------------------------------------------------------------------

def get_tg_effective_limits(inst: TelegramInstance) -> tuple[int, int]:
    """Return (effective_daily_limit, effective_hourly_limit) applying warmup caps."""
    now = datetime.now(timezone.utc)
    created = inst.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = (now - created).days

    for max_age, daily_cap, hourly_cap in TG_WARMUP_SCHEDULE:
        if age_days < max_age:
            eff_daily  = min(inst.daily_limit,  daily_cap)
            eff_hourly = min(inst.hourly_limit, hourly_cap)
            if eff_daily < inst.daily_limit or eff_hourly < inst.hourly_limit:
                logger.debug(
                    "[TGWarmup] %s age=%dd → daily=%d (cap %d), hourly=%d (cap %d)",
                    inst.phone_number, age_days,
                    eff_daily, daily_cap, eff_hourly, hourly_cap,
                )
            return eff_daily, eff_hourly

    return inst.daily_limit, inst.hourly_limit


def get_tg_warmup_status(inst: TelegramInstance) -> dict:
    """Return warmup info dict for the /api/telegram/instances endpoint."""
    now = datetime.now(timezone.utc)
    created = inst.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = (now - created).days

    in_warmup = age_days < 30
    days_remaining = max(0, 30 - age_days)
    eff_daily, eff_hourly = get_tg_effective_limits(inst)

    return {
        "in_warmup":      in_warmup,
        "age_days":       age_days,
        "days_remaining": days_remaining,
        "eff_daily":      eff_daily,
        "eff_hourly":     eff_hourly,
    }


# ---------------------------------------------------------------------------
# Rest-day enforcement (same 3-consecutive-days rule as WhatsApp)
# ---------------------------------------------------------------------------

async def _needs_rest_day(db: AsyncSession, phone_number: str) -> bool:
    """
    Return True if this instance sent on all 3 of the last 3 calendar days (UTC).
    Cached per instance per UTC day.
    """
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cached = _rest_cache.get(phone_number)
    if cached and cached[0] == today_str:
        return cached[1]

    now = datetime.now(timezone.utc)
    sent_days = 0
    for offset in range(1, 4):  # check day-1, day-2, day-3
        day_start = (now - timedelta(days=offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_end = day_start + timedelta(days=1)
        res = await db.execute(
            select(func.count(TelegramMessage.id))
            .join(TelegramInstance, TelegramMessage.instance_id == TelegramInstance.id)
            .where(
                TelegramInstance.phone_number == phone_number,
                TelegramMessage.direction == "outbound",
                TelegramMessage.created_at >= day_start,
                TelegramMessage.created_at < day_end,
            )
        )
        if (res.scalar_one() or 0) > 0:
            sent_days += 1

    needs_rest = (sent_days >= 3)
    _rest_cache[phone_number] = (today_str, needs_rest)
    if needs_rest:
        logger.warning(
            "[TGRestDay] %s sent on 3 consecutive days — skipping today",
            phone_number,
        )
    return needs_rest


# ---------------------------------------------------------------------------
# In-memory rate helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _prune(phone: str) -> None:
    cutoff = _now_utc() - timedelta(hours=24)
    _send_log[phone] = [ts for ts in _send_log[phone] if ts > cutoff]


def _hourly_count(phone: str) -> int:
    cutoff = _now_utc() - timedelta(hours=1)
    return sum(1 for ts in _send_log[phone] if ts > cutoff)


def _daily_count(phone: str) -> int:
    return len(_send_log[phone])


# ---------------------------------------------------------------------------
# Main instance selector
# ---------------------------------------------------------------------------

async def get_best_tg_instance(
    db: AsyncSession,
    is_reservation: bool = True,
) -> Optional[TelegramInstance]:
    """
    Select the best available TelegramInstance applying warmup, rest-day,
    FloodWait, and reply/block rate rules.

    3-pass greedy selection (mirrors WhatsApp pool):
      Pass 0: safe only (no danger zone, no rest, flood_count < 3)
      Pass 1: allow danger zone, still respect rest days
      Pass 2: all constraints lifted (last resort)
    """
    async with _lock:
        result = await db.execute(
            select(TelegramInstance).where(
                TelegramInstance.is_active == True,
                TelegramInstance.is_authorized == True,
                TelegramInstance.is_banned == False,
                TelegramInstance.health_status.notin_(
                    ["deactivated", "session_expired", "banned"]
                ),
            ).order_by(
                TelegramInstance.last_send_at.asc().nulls_first(),
                TelegramInstance.created_at.desc(),
            )
        )
        instances = list(result.scalars().all())

        if not instances:
            return None

        # Pre-fetch health signals
        reply_rates: dict[str, Optional[float]] = {}
        block_rates: dict[str, Optional[float]] = {}
        rest_flags:  dict[str, bool] = {}

        for inst in instances:
            p = inst.phone_number
            try:
                reply_rates[p] = await get_tg_reply_rate(db, p)
            except Exception:
                reply_rates[p] = None
            try:
                block_rates[p] = await get_tg_block_rate(db, p)
            except Exception:
                block_rates[p] = None
            try:
                rest_flags[p] = await _needs_rest_day(db, p)
            except Exception:
                rest_flags[p] = False

        best: Optional[TelegramInstance] = None
        best_hourly: Optional[int] = None

        for pass_num in range(3):
            for inst in instances:
                p = inst.phone_number
                _prune(p)
                hourly = _hourly_count(p)
                daily  = _daily_count(p)

                eff_daily, eff_hourly = get_tg_effective_limits(inst)
                if hourly >= eff_hourly:
                    continue
                if daily >= eff_daily:
                    continue

                rr = reply_rates.get(p)
                br = block_rates.get(p)
                in_reply_danger = rr is not None and rr < TG_REPLY_RATE_DANGER
                in_block_danger = br is not None and br > TG_BLOCK_RATE_DANGER
                needs_rest      = rest_flags.get(p, False)
                # Flood-prone instances are deprioritised on passes 0 and 1
                flood_prone     = inst.flood_wait_count >= 3

                if pass_num == 0:
                    if in_reply_danger or in_block_danger or needs_rest or flood_prone:
                        continue
                elif pass_num == 1:
                    if needs_rest:
                        continue

                if best is None or hourly < best_hourly:
                    best = inst
                    best_hourly = hourly

            if best is not None:
                break

        # Reserve send slot (update last_send_at with gap so next call waits)
        if best and is_reservation:
            now = _now_utc()
            gap_seconds = random.uniform(90, 180)
            gap = max(gap_seconds, best.min_delay_sec)
            prev_at = best.last_send_at or (now - timedelta(seconds=gap + 1))
            if prev_at.tzinfo is None:
                prev_at = prev_at.replace(tzinfo=timezone.utc)
            best.last_send_at = max(now, prev_at + timedelta(seconds=gap))
            await db.commit()
            await db.refresh(best)

        return best


# ---------------------------------------------------------------------------
# Helpers called from sender.py and health_monitor.py
# ---------------------------------------------------------------------------

async def record_tg_send(phone_number: str) -> None:
    async with _lock:
        _send_log[phone_number].append(_now_utc())


async def mark_tg_banned(db: AsyncSession, phone_number: str) -> None:
    from app.services.telegram.client_manager import disconnect_client
    async with _lock:
        await db.execute(
            update(TelegramInstance)
            .where(TelegramInstance.phone_number == phone_number)
            .values(is_banned=True, is_active=False, health_status="deactivated")
        )
        await db.commit()
        _send_log.pop(phone_number, None)
        _rest_cache.pop(phone_number, None)
        logger.warning(f"[TGPool] {phone_number} marked as banned and removed from pool")
    await disconnect_client(phone_number)


def get_tg_instance_stats() -> dict:
    now = _now_utc()
    stats = {}
    for phone, timestamps in _send_log.items():
        hourly = sum(1 for ts in timestamps if ts > now - timedelta(hours=1))
        stats[phone] = {"hourly": hourly, "daily": len(timestamps)}
    return stats
