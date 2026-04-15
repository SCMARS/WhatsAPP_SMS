"""
One-time migration script — adds nudge_sent and first_contact_at to conversations.

What it does:
  1. ADD COLUMN conversations.nudge_sent (bool, default false)
  2. ADD COLUMN conversations.first_contact_at (timestamptz, nullable)

Safe to re-run: each step uses ADD COLUMN IF NOT EXISTS.

Usage:
    python migrate_split_message.py
"""

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def run_migration() -> None:
    engine = create_async_engine(settings.async_database_url, echo=False)

    async with engine.begin() as conn:

        # 1. Add nudge_sent column
        try:
            await conn.execute(text(
                "ALTER TABLE conversations "
                "ADD COLUMN IF NOT EXISTS nudge_sent BOOLEAN NOT NULL DEFAULT false"
            ))
            logger.info("Step 1: nudge_sent column added (or already existed)")
        except Exception as e:
            logger.error(f"Step 1 failed: {e}")
            raise

        # 2. Add first_contact_at column
        try:
            await conn.execute(text(
                "ALTER TABLE conversations "
                "ADD COLUMN IF NOT EXISTS first_contact_at TIMESTAMPTZ NULL"
            ))
            logger.info("Step 2: first_contact_at column added (or already existed)")
        except Exception as e:
            logger.error(f"Step 2 failed: {e}")
            raise

    await engine.dispose()
    logger.info("Migration complete.")


if __name__ == "__main__":
    asyncio.run(run_migration())
