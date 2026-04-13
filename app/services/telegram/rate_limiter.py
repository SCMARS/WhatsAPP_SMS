"""
Telegram rate limiter — thin wrapper over the shared rate_limiter utilities.

Reuses platform-agnostic functions from app.services.rate_limiter and overrides
only the Telegram-specific footer.
"""

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Optional

from app.services.rate_limiter import (  # noqa: F401 — re-export for callers
    add_footer,
    batch_pause,
    calc_typing_time,
    insert_zero_width,
    reply_pause,
)
from app.db.models import TelegramInstance

logger = logging.getLogger(__name__)


def add_tg_footer(text: str) -> str:
    return text


async def tg_wait_before_send(instance: TelegramInstance) -> None:
    """
    Wait until the per-instance cooldown reservation expires.
    Mirrors wait_before_send() from rate_limiter.py but uses TelegramInstance.
    """
    last = instance.last_send_at
    if last is None:
        return
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    jitter = random.uniform(2, 10)
    target = last.timestamp() + jitter
    wait = target - now.timestamp()
    if wait > 0:
        logger.debug("[TGRateLimiter] Waiting %.1fs before send on %s", wait, instance.phone_number)
        await asyncio.sleep(wait)
