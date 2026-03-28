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


async def reply_pause(min_sec: float = 2.0, max_sec: float = 5.0) -> None:
    """Short human-like typing delay before sending an AI reply."""
    delay = random.uniform(min_sec, max_sec)
    logger.debug(f"Reply pause: {delay:.1f}s")
    await asyncio.sleep(delay)


async def wait_before_send(instance: WhatsAppInstance) -> None:
    min_sec = instance.min_delay_sec
    max_sec = instance.max_delay_sec
    mu = (min_sec + max_sec) / 2
    sigma = (max_sec - min_sec) / 6
    delay = random.gauss(mu=mu, sigma=sigma)
    delay = max(min_sec, min(max_sec, delay))
    logger.debug(f"Waiting {delay:.1f}s before send (instance {instance.instance_id})")
    await asyncio.sleep(delay)


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
