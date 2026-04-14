import asyncio
import logging
import random
import re
from typing import Optional

from app.db.models import WhatsAppInstance

logger = logging.getLogger(__name__)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _index_in_spans(index: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in spans)


def insert_zero_width(text: str) -> str:
    """
    Insert a single \u200b (zero-width space) on a safe whitespace boundary.
    This keeps anti-dedup variation without breaking URLs or promo codes.
    """
    if not text or random.random() >= 0.3:
        return text

    min_idx = max(1, len(text) // 5)
    max_idx = max(min_idx, (len(text) * 4) // 5)
    protected_spans = [match.span() for match in _URL_RE.finditer(text)]
    safe_positions = [
        idx
        for idx, ch in enumerate(text)
        if ch.isspace()
        and min_idx <= idx <= max_idx
        and not _index_in_spans(idx, protected_spans)
        and not _index_in_spans(max(0, idx - 1), protected_spans)
    ]
    if not safe_positions:
        return text

    insert_at = random.choice(safe_positions)
    return text[:insert_at] + "\u200b" + text[insert_at:]


def spin_text(template: str, contact: Optional[dict] = None) -> str:
    """Backward-compatible alias; now only adds zero-width uniqueness."""
    return insert_zero_width(template.strip())


def add_footer(text: str) -> str:
    """Outbound messages are sent as-is without any auto-appended footer."""
    return text


async def reply_pause(min_sec: float = 6.0, max_sec: float = 12.0) -> None:
    """Human-like pause between consecutive messages in a sequence."""
    delay = random.uniform(min_sec, max_sec)
    logger.debug(f"Reply pause: {delay:.1f}s")
    await asyncio.sleep(delay)


async def initial_compose_pause(min_sec: float = 8.0, max_sec: float = 18.0) -> None:
    """Extra thinking delay before the first outbound message to reduce bot-like speed."""
    delay = random.uniform(min_sec, max_sec)
    logger.debug(f"Initial compose pause: {delay:.1f}s")
    await asyncio.sleep(delay)


async def wait_before_send(instance: WhatsAppInstance) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    target = instance.last_send_at
    if not target:
        return

    wait = (target - now).total_seconds()
    if wait > 0:
        # Add random jitter so we don't send *exactly* at 240.00s every time
        jitter = random.uniform(2.0, 10.0)
        actual_wait = wait + jitter
        logger.info(f"Instance {instance.instance_id} is in cooldown, waiting {actual_wait:.1f}s")
        await asyncio.sleep(actual_wait)


async def batch_pause(batch_index: int, batch_size: int = 10, pause_sec: float = 120.0) -> None:
    if batch_index > 0 and batch_index % batch_size == 0:
        jitter = random.uniform(-0.2, 0.2)
        actual_pause = pause_sec * (1 + jitter)
        logger.info(f"Batch pause after {batch_index} messages: {actual_pause:.1f}s")
        await asyncio.sleep(actual_pause)


def calc_typing_time(message: str) -> int:
    """
    Estimate realistic typing time in milliseconds.
    - 10% fast   : 5 000 –  9 000 ms
    - 75% normal : 8 000 – 22 000 ms
    - 15% slow   :18 000 – 32 000 ms

    Minimum is 5 000 ms even for very short messages so the typing indicator
    is always visible to the recipient.
    """
    chars = len(message)
    base_ms = int((chars / 4.4) * 1000)  # ~4.4 chars/sec baseline

    r = random.random()
    if r < 0.10:
        # Fast bucket
        ms = int(base_ms * random.uniform(0.7, 0.95))
        return max(5000, min(ms, 9000))
    elif r < 0.25:
        # Slow bucket
        ms = int(base_ms * random.uniform(1.7, 2.8))
        return max(18000, min(ms, 32000))
    else:
        # Normal bucket
        ms = int(base_ms * random.uniform(0.9, 1.25))
        return max(8000, min(ms, 22000))


# ---------------------------------------------------------------------------
# Legacy helper kept for any external callers that haven't migrated to spin_text
# ---------------------------------------------------------------------------

def personalize_message(template: str, lead_name: Optional[str] = None) -> str:
    message = template.replace("{{greeting}}", "Hi")
    if lead_name:
        message = message.replace("{{name}}", lead_name)
    else:
        message = message.replace("{{name}}", "")
    return message.strip()
