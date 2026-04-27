"""Monitor Telegram message view status every 10 hours."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, and_

from app.db.models import TelegramMessage
from app.db.database import get_db

logger = logging.getLogger(__name__)
TZ = timezone.utc


async def check_message_views() -> dict:
    """Check and update viewed status for recent messages."""
    
    async for db in get_db():
        # Find messages to check:
        # - Outbound direction
        # - Status "sent" (successfully sent)
        # - viewed_at is NULL (not yet marked as viewed)
        # - check_until is NULL or past now (should still be checked)
        # - Less than 4 days old without reply
        
        four_days_ago = datetime.now(TZ) - timedelta(days=4)
        
        messages = await db.execute(
            select(TelegramMessage).where(
                and_(
                    TelegramMessage.direction == "outbound",
                    TelegramMessage.status == "sent",
                    TelegramMessage.viewed_at.is_(None),
                    TelegramMessage.replied_at.is_(None),
                    TelegramMessage.created_at > four_days_ago,
                )
            )
        )
        
        to_check = messages.scalars().all()
        
        if not to_check:
            logger.info("[MessageTracker] No messages to check")
            return {"checked": 0, "updated": 0}
        
        logger.info(f"[MessageTracker] Checking {len(to_check)} messages")
        
        # For each message, check if it was read in Telegram
        # (This would call Telegram API via Telethon client)
        updated_count = 0
        
        for msg in to_check:
            try:
                # TODO: Call Telegram API to check if message was read
                # For now, simulate the check
                
                # If message is older than 4 days and no reply:
                if msg.created_at < four_days_ago and not msg.replied_at:
                    msg.check_until = datetime.now(TZ)
                    logger.info(f"[MessageTracker] Expired message {msg.id}")
                    updated_count += 1
                    
            except Exception as e:
                logger.error(f"[MessageTracker] Error checking message {msg.id}: {e}")
        
        await db.commit()
        
        return {"checked": len(to_check), "updated": updated_count}


async def run_message_tracker(stop_event: asyncio.Event) -> None:
    """Background task: check message views every 10 hours."""
    
    logger.info("[MessageTracker] Starting")
    
    while not stop_event.is_set():
        try:
            # Run every 10 hours
            await asyncio.sleep(10 * 3600)
            
            if stop_event.is_set():
                break
            
            result = await check_message_views()
            logger.info(f"[MessageTracker] Result: {result}")
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[MessageTracker] Error: {e}")
    
    logger.info("[MessageTracker] Stopped")
