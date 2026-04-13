import asyncio
import logging
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import WhatsAppInstance, WhatsAppMessage
from app.services.reply_monitor import (
    BLOCK_RATE_DANGER,
    REPLY_RATE_DANGER,
    get_block_rate,
    get_reply_rate,
)

logger = logging.getLogger(__name__)

# In-memory send log: instance_id -> list of send datetimes
_send_log: dict[str, list[datetime]] = defaultdict(list)
_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# 1. WARM-UP SCHEDULE
# New phone numbers are far more likely to be banned at high volume.
# We cap effective daily/hourly limits during the warm-up period.
# Format: (max_age_days, daily_cap, hourly_cap)
# After day 30 the instance's own configured limits apply.
# ---------------------------------------------------------------------------
WARMUP_SCHEDULE: list[tuple[int, int, int]] = [
    (7,  30,   5),   # Week 1: max 30/day,  5/hour
    (14, 60,  10),   # Week 2: max 60/day, 10/hour
    (21, 100, 20),   # Week 3: max 100/day, 20/hour
    (30, 150, 25),   # Week 4: max 150/day, 25/hour
    # After day 30 → use configured daily_limit / hourly_limit
]

# Cache for rest-day check: instance_id -> (utc_date_str, needs_rest)
_rest_cache: dict[str, tuple[str, bool]] = {}


# ---------------------------------------------------------------------------
# Warmup helpers
# ---------------------------------------------------------------------------

def get_effective_limits(inst: WhatsAppInstance) -> tuple[int, int]:
    """
    Return (effective_daily_limit, effective_hourly_limit) for this instance,
    applying the warm-up schedule for new numbers.

    During warm-up the caps are applied as hard maximums:
      effective = min(configured_limit, warmup_cap)
    After day 30 the configured limits are used unchanged.
    """
    now = datetime.now(timezone.utc)
    # created_at may be naive (SQLite tests); normalise to UTC-aware
    created = inst.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = (now - created).days

    for max_age, daily_cap, hourly_cap in WARMUP_SCHEDULE:
        if age_days < max_age:
            effective_daily  = min(inst.daily_limit,  daily_cap)
            effective_hourly = min(inst.hourly_limit, hourly_cap)
            if effective_daily < inst.daily_limit or effective_hourly < inst.hourly_limit:
                logger.debug(
                    "[Warmup] %s age=%dd → daily=%d (cap %d), hourly=%d (cap %d)",
                    inst.instance_id, age_days,
                    effective_daily, daily_cap, effective_hourly, hourly_cap,
                )
            return effective_daily, effective_hourly

    return inst.daily_limit, inst.hourly_limit


def get_warmup_status(inst: WhatsAppInstance) -> dict:
    """Return warmup info dict for reporting (API / health monitor)."""
    now = datetime.now(timezone.utc)
    created = inst.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = (now - created).days

    in_warmup = age_days < 30
    days_remaining = max(0, 30 - age_days)
    eff_daily, eff_hourly = get_effective_limits(inst)

    return {
        "in_warmup":      in_warmup,
        "age_days":       age_days,
        "days_remaining": days_remaining,
        "eff_daily":      eff_daily,
        "eff_hourly":     eff_hourly,
    }


# ---------------------------------------------------------------------------
# 2. REST DAYS
# Green API recommends: do NOT send more than 3 consecutive days without rest.
# We check if this instance sent outbound messages on each of the last 3 calendar
# days (UTC). If yes → needs_rest = True → skip today.
# ---------------------------------------------------------------------------

async def _needs_rest_day(db: AsyncSession, inst_id: str) -> bool:
    """
    Return True if this instance sent on all 3 of the last 3 calendar days (UTC).
    Result is cached per instance per UTC day to avoid repeated DB hits.
    """
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Use cached result if it's from today
    cached = _rest_cache.get(inst_id)
    if cached and cached[0] == today_str:
        return cached[1]

    now = datetime.now(timezone.utc)
    sent_days = 0
    for offset in range(1, 4):   # check day-1, day-2, day-3
        day_start = (now - timedelta(days=offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_end = day_start + timedelta(days=1)
        res = await db.execute(
            select(func.count(WhatsAppMessage.id))
            .join(WhatsAppInstance, WhatsAppMessage.instance_id == WhatsAppInstance.id)
            .where(
                WhatsAppInstance.instance_id == inst_id,
                WhatsAppMessage.direction == "outbound",
                WhatsAppMessage.created_at >= day_start,
                WhatsAppMessage.created_at < day_end,
            )
        )
        if (res.scalar_one() or 0) > 0:
            sent_days += 1

    needs_rest = (sent_days >= 3)
    _rest_cache[inst_id] = (today_str, needs_rest)

    if needs_rest:
        logger.warning(
            "[RestDay] %s sent on 3 consecutive days — skipping today to protect reputation",
            inst_id,
        )
    return needs_rest


# ---------------------------------------------------------------------------
# Internal rate-limit helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _prune(instance_id: str) -> None:
    cutoff = _now_utc() - timedelta(hours=24)
    _send_log[instance_id] = [ts for ts in _send_log[instance_id] if ts > cutoff]


def _hourly_count(instance_id: str) -> int:
    cutoff = _now_utc() - timedelta(hours=1)
    return sum(1 for ts in _send_log[instance_id] if ts > cutoff)


def _daily_count(instance_id: str) -> int:
    return len(_send_log[instance_id])


# ---------------------------------------------------------------------------
# 3. Main instance selector — applies warmup + rest-day + reply/block rate
# ---------------------------------------------------------------------------

async def get_best_instance(
    db: AsyncSession, is_reservation: bool = True
) -> Optional[WhatsAppInstance]:
    async with _lock:
        result = await db.execute(
            select(WhatsAppInstance).where(
                WhatsAppInstance.is_active == True,
                WhatsAppInstance.is_banned == False,
                WhatsAppInstance.health_status.notin_(
                    ["blocked", "notAuthorized", "yellowCard"]
                ),
            ).order_by(
                WhatsAppInstance.last_send_at.asc().nulls_first(),
                WhatsAppInstance.created_at.desc(),
            )
        )
        instances = list(result.scalars().all())

        if not instances:
            return None

        # ── Preferred instance (hard-configured override) ──────────────────
        preferred_id = (getattr(settings, "INSTANCE_ID", "") or "").strip()
        if preferred_id:
            for inst in instances:
                if inst.instance_id != preferred_id:
                    continue
                _prune(inst.instance_id)
                eff_daily, eff_hourly = get_effective_limits(inst)
                if _hourly_count(inst.instance_id) >= eff_hourly:
                    break
                if _daily_count(inst.instance_id) >= eff_daily:
                    break
                # Even preferred instance obeys rest-day rule
                if await _needs_rest_day(db, inst.instance_id):
                    break
                return inst

        # ── Pre-fetch health signals for all candidates ────────────────────
        reply_rates: dict[str, Optional[float]] = {}
        block_rates: dict[str, Optional[float]] = {}
        rest_flags:  dict[str, bool] = {}

        for inst in instances:
            iid = inst.instance_id
            try:
                reply_rates[iid] = await get_reply_rate(db, iid)
            except Exception:
                reply_rates[iid] = None
            try:
                block_rates[iid] = await get_block_rate(db, iid)
            except Exception:
                block_rates[iid] = None
            try:
                rest_flags[iid] = await _needs_rest_day(db, iid)
            except Exception:
                rest_flags[iid] = False

        best: Optional[WhatsAppInstance] = None
        best_hourly: Optional[int] = None

        # Two-pass selection:
        #   Pass 1: skip danger-zone (reply OR block) AND resting instances
        #   Pass 2: allow danger-zone if no safe alternative exists
        #           (never skip resting instances on pass 2 — rest is mandatory
        #            when there IS an alternative, but if ALL need rest we send anyway)
        for pass_num in range(3):
            for inst in instances:
                iid = inst.instance_id
                _prune(iid)
                hourly = _hourly_count(iid)
                daily  = _daily_count(iid)

                eff_daily, eff_hourly = get_effective_limits(inst)

                if hourly >= eff_hourly:
                    continue
                if daily >= eff_daily:
                    continue

                rr = reply_rates.get(iid)
                br = block_rates.get(iid)
                in_reply_danger = rr is not None and rr < REPLY_RATE_DANGER
                in_block_danger = br is not None and br > BLOCK_RATE_DANGER
                needs_rest      = rest_flags.get(iid, False)

                if pass_num == 0:
                    # Safe only: no danger zone, no rest needed
                    if in_reply_danger or in_block_danger or needs_rest:
                        continue
                elif pass_num == 1:
                    # Allow danger zone, but still respect rest days
                    if needs_rest:
                        continue
                # pass_num == 2: all constraints lifted (last resort)

                if best is None or hourly < best_hourly:
                    best = inst
                    best_hourly = hourly

            if best is not None:
                break

        # ── Reserve send slot ───────────────────────────────────────────────
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

async def record_send(instance_id: str) -> None:
    async with _lock:
        _send_log[instance_id].append(_now_utc())


async def mark_banned(db: AsyncSession, instance_id: str) -> None:
    async with _lock:
        await db.execute(
            update(WhatsAppInstance)
            .where(WhatsAppInstance.instance_id == instance_id)
            .values(is_banned=True, is_active=False)
        )
        await db.commit()
        _send_log.pop(instance_id, None)
        _rest_cache.pop(instance_id, None)
        logger.warning(f"Instance {instance_id} marked as banned and removed from pool")


def get_instance_stats() -> dict:
    stats = {}
    now = _now_utc()
    for instance_id, timestamps in _send_log.items():
        hourly = sum(1 for ts in timestamps if ts > now - timedelta(hours=1))
        daily  = len(timestamps)
        stats[instance_id] = {"hourly": hourly, "daily": daily}
    return stats
