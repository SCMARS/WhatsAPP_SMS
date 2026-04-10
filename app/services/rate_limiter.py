import asyncio
import logging
import random
import secrets
from typing import Optional

from app.db.models import WhatsAppInstance

logger = logging.getLogger(__name__)

def insert_zero_width(text: str) -> str:
    """Insert invisible per-message fingerprint for transport-level uniqueness."""
    if not text:
        return text
    # Encode a random 12-bit fingerprint as a 3-char invisible sequence.
    # This keeps content human-identical while making transport payload unique.
    alphabet = ["\u200b", "\u200c", "\u200d", "\ufeff"]
    value = secrets.randbelow(4096)  # 12 bits
    chars = []
    for _ in range(3):
        chars.append(alphabet[value & 0b11])
        value >>= 2
    marker = "".join(chars)

    insert_at = random.randint(max(0, len(text) // 5), max(0, (len(text) * 4) // 5))
    return text[:insert_at] + marker + text[insert_at:]


def spin_text(template: str, contact: dict = {}) -> str:
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
    Estimate realistic typing time in milliseconds with stable target distribution:
    - 10% fast  (<= 2100 ms)
    - 80% normal (2101..5999 ms)
    - 10% slow  (>= 6000 ms)
    """
    chars = len(message)
    base_ms = int((chars / 3) * 1000)  # baseline around 3 chars/sec

    r = random.random()
    if r < 0.10:
        # Fast bucket: always <= 2100
        ms = int(base_ms * random.uniform(0.35, 0.6))
        return max(1200, min(ms, 2100))
    elif r < 0.20:
        # Slow bucket: always >= 6000
        ms = int(base_ms * random.uniform(1.8, 3.4))
        return max(6000, min(ms, 12000))
    else:
        # Normal bucket: strictly between fast and slow thresholds
        ms = int(base_ms * random.uniform(0.8, 1.25))
        return max(2101, min(ms, 5999))


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
