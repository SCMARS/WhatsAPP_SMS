import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import WhatsAppInstance

logger = logging.getLogger(__name__)

# In-memory send log: instance_id -> list of send datetimes
_send_log: dict[str, list[datetime]] = defaultdict(list)
_lock = asyncio.Lock()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _prune(instance_id: str) -> None:
    now = _now_utc()
    cutoff_hourly = now - timedelta(hours=1)
    cutoff_daily = now - timedelta(hours=24)
    # Keep only entries within the last 24h (daily window is largest)
    _send_log[instance_id] = [
        ts for ts in _send_log[instance_id] if ts > cutoff_daily
    ]


def _hourly_count(instance_id: str) -> int:
    now = _now_utc()
    cutoff = now - timedelta(hours=1)
    return sum(1 for ts in _send_log[instance_id] if ts > cutoff)


def _daily_count(instance_id: str) -> int:
    return len(_send_log[instance_id])


async def get_best_instance(db: AsyncSession, is_reservation: bool = True) -> Optional[WhatsAppInstance]:
    async with _lock:
        result = await db.execute(
            select(WhatsAppInstance).where(
                WhatsAppInstance.is_active == True,
                WhatsAppInstance.is_banned == False,
                WhatsAppInstance.health_status.notin_(["blocked", "notAuthorized", "yellowCard"]),
            ).order_by(
                WhatsAppInstance.last_send_at.asc().nulls_first(),
                WhatsAppInstance.created_at.desc()
            )
        )
        instances = result.scalars().all()

        if not instances:
            return None

        # Prefer explicitly configured instance when available.
        preferred_id = (getattr(settings, "INSTANCE_ID", "") or "").strip()
        if preferred_id:
            for inst in instances:
                if inst.instance_id != preferred_id:
                    continue
                _prune(inst.instance_id)
                if _hourly_count(inst.instance_id) >= inst.hourly_limit:
                    break
                if _daily_count(inst.instance_id) >= inst.daily_limit:
                    break
                return inst

        best: Optional[WhatsAppInstance] = None
        best_hourly = None

        for inst in instances:
            _prune(inst.instance_id)
            hourly = _hourly_count(inst.instance_id)
            daily = _daily_count(inst.instance_id)

            if hourly >= inst.hourly_limit:
                continue
            if daily >= inst.daily_limit:
                continue

            if best is None or hourly < best_hourly:
                best = inst
                best_hourly = hourly

        if best and is_reservation:
            now = _now_utc()
            # Pro Sender: Random gap between 90 and 180 seconds for human-like pattern
            import random
            gap_seconds = random.uniform(90, 180)
            
            # Use instance min_delay_sec if it's larger for custom tuning.
            gap = max(gap_seconds, best.min_delay_sec)

            # Previous reservation
            prev_at = best.last_send_at or (now - timedelta(seconds=gap + 1))

            # New reservation: at least 'gap' seconds from the previous reservation/send.
            best.last_send_at = max(now, prev_at + timedelta(seconds=gap))

            await db.commit()
            await db.refresh(best)

        return best


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
        logger.warning(f"Instance {instance_id} marked as banned and removed from pool")


def get_instance_stats() -> dict:
    stats = {}
    for instance_id, timestamps in _send_log.items():
        now = _now_utc()
        hourly = sum(1 for ts in timestamps if ts > now - timedelta(hours=1))
        daily = len(timestamps)
        stats[instance_id] = {"hourly": hourly, "daily": daily}
    return stats
