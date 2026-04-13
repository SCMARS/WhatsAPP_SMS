import asyncio
import logging
import random
from typing import Optional

from app.db.models import WhatsAppInstance

logger = logging.getLogger(__name__)

def insert_zero_width(text: str) -> str:
    """
    Insert a single \u200b (zero-width space) at a random position — but only
    30% of the time.  Using it on every message creates a detectable pattern;
    sparse insertion is enough to break hash-dedup without raising flags.
    """
    if not text or random.random() >= 0.3:
        return text
    insert_at = random.randint(max(0, len(text) // 5), max(0, (len(text) * 4) // 5))
    return text[:insert_at] + "\u200b" + text[insert_at:]


def spin_text(template: str, contact: Optional[dict] = None) -> str:
    """Backward-compatible alias; now only adds zero-width uniqueness."""
    return insert_zero_width(template.strip())


def add_footer(text: str) -> str:
    """Append opt-out instructions to outbound broadcast messages."""
    return text + "\n\n_Щоб відписатись — відповідайте 'Стоп'_"


async def reply_pause(min_sec: float = 4.0, max_sec: float = 8.0) -> None:
    """Short human-like typing delay before sending next message in split sequence."""
    delay = random.uniform(min_sec, max_sec)
    logger.debug(f"Reply pause: {delay:.1f}s")
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
    - 10% fast   : 4 000 –  6 000 ms
    - 80% normal : 6 000 – 15 000 ms
    - 10% slow   :15 000 – 25 000 ms

    Minimum is 4 000 ms even for very short messages so the typing indicator
    is always visible to the recipient.
    """
    chars = len(message)
    base_ms = int((chars / 4) * 1000)  # ~4 chars/sec baseline

    r = random.random()
    if r < 0.10:
        # Fast bucket
        ms = int(base_ms * random.uniform(0.6, 0.9))
        return max(4000, min(ms, 6000))
    elif r < 0.20:
        # Slow bucket
        ms = int(base_ms * random.uniform(1.8, 3.0))
        return max(15000, min(ms, 25000))
    else:
        # Normal bucket
        ms = int(base_ms * random.uniform(0.9, 1.3))
        return max(6000, min(ms, 15000))


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
