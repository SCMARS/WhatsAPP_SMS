"""
One-time migration script — run ONCE on an existing DB to add Telegram support.

What it does:
  1. ADD COLUMN conversations.platform (varchar, default 'whatsapp')
  2. DROP old unique constraint uq_conversation_campaign_phone
  3. CREATE new constraint uq_conversation_campaign_phone_platform (campaign_id, phone, platform)
  4. CREATE TABLE telegram_instances
  5. CREATE TABLE telegram_messages

Safe to re-run: each step is guarded by "if not exists" / try-except.

Usage:
    python migrate_telegram.py

Requires DATABASE_URL in .env (or environment).
"""

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.db.models import Base

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def run_migration() -> None:
    engine = create_async_engine(settings.async_database_url, echo=False)

    async with engine.begin() as conn:

        # 1. Add platform column (idempotent)
        try:
            await conn.execute(text(
                "ALTER TABLE conversations "
                "ADD COLUMN IF NOT EXISTS platform VARCHAR(20) NOT NULL DEFAULT 'whatsapp'"
            ))
            logger.info("Step 1: platform column added (or already existed)")
        except Exception as e:
            logger.error(f"Step 1 failed: {e}")
            raise

        # 2. Drop old 2-column unique constraint (may not exist on fresh DBs)
        try:
            await conn.execute(text(
                "ALTER TABLE conversations "
                "DROP CONSTRAINT IF EXISTS uq_conversation_campaign_phone"
            ))
            logger.info("Step 2: old unique constraint dropped (or did not exist)")
        except Exception as e:
            logger.warning(f"Step 2 warning: {e}")

        # 3. Create new 3-column unique constraint (idempotent)
        try:
            await conn.execute(text(
                "ALTER TABLE conversations "
                "ADD CONSTRAINT uq_conversation_campaign_phone_platform "
                "UNIQUE (campaign_id, phone, platform)"
            ))
            logger.info("Step 3: new unique constraint created")
        except Exception as e:
            if "already exists" in str(e).lower():
                logger.info("Step 3: constraint already exists, skipping")
            else:
                logger.error(f"Step 3 failed: {e}")
                raise

        # 4 & 5. Create new tables via SQLAlchemy metadata (create_all is additive)
        await conn.run_sync(Base.metadata.create_all)
        logger.info("Steps 4-5: telegram_instances and telegram_messages tables created (or already existed)")

    await engine.dispose()
    logger.info("Migration complete.")


if __name__ == "__main__":
    asyncio.run(run_migration())
