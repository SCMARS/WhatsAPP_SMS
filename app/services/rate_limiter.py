import asyncio
import logging
import random
from typing import Optional

from app.db.models import WhatsAppInstance

logger = logging.getLogger(__name__)

GREETINGS = [
    "Hi",
    "Hey",
    "Hello",
    "Hi there",
    "Hey there",
]


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


def personalize_message(template: str, lead_name: Optional[str] = None) -> str:
    greeting = random.choice(GREETINGS)
    message = template.replace("{{greeting}}", greeting)
    if lead_name:
        message = message.replace("{{name}}", lead_name)
    else:
        message = message.replace("{{name}}", "")
    return message.strip()
