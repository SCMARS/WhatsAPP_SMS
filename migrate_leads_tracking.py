"""
Migration: Add lead conversion tracking columns to conversations + create lead_events table.

Run once against the live DB:
    python migrate_leads_tracking.py

Safe to re-run — all DDL uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.
"""

import asyncio
import logging
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set")

# Railway / Heroku give postgres:// — asyncpg needs postgresql+asyncpg://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+asyncpg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)


async def run_migration() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:

        # ── 1. New columns on conversations ──────────────────────────────────
        steps = [
            (
                "lead_status column",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS lead_status VARCHAR(30) NOT NULL DEFAULT 'new'",
            ),
            (
                "outbound_count column",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS outbound_count INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "reply_count column",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS reply_count INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "replied_at column",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS replied_at TIMESTAMPTZ NULL",
            ),
            (
                "last_activity_at column",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ NULL",
            ),
            (
                "notes column",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS notes TEXT NULL",
            ),
            (
                "assigned_link_url column",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS assigned_link_url TEXT NULL",
            ),
        ]

        for name, sql in steps:
            await conn.execute(text(sql))
            logger.info("Step done: %s", name)

        # ── 2. Backfill outbound_count from existing messages ─────────────────
        await conn.execute(text("""
            UPDATE conversations c
            SET outbound_count = (
                SELECT COUNT(*) FROM whatsapp_messages m
                WHERE m.conversation_id = c.id AND m.direction = 'outbound'
            )
            WHERE outbound_count = 0
        """))
        logger.info("Backfilled outbound_count from whatsapp_messages")

        # ── 3. Backfill reply_count + replied_at from existing messages ───────
        await conn.execute(text("""
            UPDATE conversations c
            SET
                reply_count = (
                    SELECT COUNT(*) FROM whatsapp_messages m
                    WHERE m.conversation_id = c.id AND m.direction = 'inbound'
                ),
                replied_at = (
                    SELECT MIN(m.created_at) FROM whatsapp_messages m
                    WHERE m.conversation_id = c.id AND m.direction = 'inbound'
                )
            WHERE reply_count = 0
        """))
        logger.info("Backfilled reply_count and replied_at from whatsapp_messages")

        # ── 4. Backfill last_activity_at ──────────────────────────────────────
        await conn.execute(text("""
            UPDATE conversations c
            SET last_activity_at = (
                SELECT MAX(m.created_at) FROM whatsapp_messages m
                WHERE m.conversation_id = c.id
            )
            WHERE last_activity_at IS NULL
        """))
        logger.info("Backfilled last_activity_at")

        # ── 5. Backfill lead_status from existing data ────────────────────────
        # unsubscribed
        await conn.execute(text("""
            UPDATE conversations SET lead_status = 'unsubscribed'
            WHERE is_blacklisted = TRUE AND lead_status = 'new'
        """))
        # replied
        await conn.execute(text("""
            UPDATE conversations SET lead_status = 'replied'
            WHERE reply_count > 0 AND lead_status = 'new'
        """))
        # contacted (sent outbound but no reply)
        await conn.execute(text("""
            UPDATE conversations SET lead_status = 'contacted'
            WHERE outbound_count > 0 AND reply_count = 0
              AND is_blacklisted = FALSE AND lead_status = 'new'
        """))
        logger.info("Backfilled lead_status")

        # ── 6. Backfill assigned_link_url from links_pool ─────────────────────
        await conn.execute(text("""
            UPDATE conversations c
            SET assigned_link_url = lp.url
            FROM links_pool lp
            WHERE lp.lead_id = c.lead_id
              AND c.assigned_link_url IS NULL
        """))
        logger.info("Backfilled assigned_link_url from links_pool")

        # ── 7. Create lead_events table ───────────────────────────────────────
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS lead_events (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                event_type VARCHAR(50) NOT NULL,
                note TEXT,
                meta JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_lead_events_conversation_created
            ON lead_events (conversation_id, created_at)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_lead_events_event_type
            ON lead_events (event_type)
        """))
        logger.info("lead_events table created (or already existed)")

        # ── 8. Seed initial 'contacted' events from first outbound messages ───
        await conn.execute(text("""
            INSERT INTO lead_events (conversation_id, event_type, note, created_at)
            SELECT DISTINCT ON (m.conversation_id)
                m.conversation_id,
                'contacted',
                'Backfilled from migration',
                m.created_at
            FROM whatsapp_messages m
            WHERE m.direction = 'outbound'
              AND NOT EXISTS (
                  SELECT 1 FROM lead_events e
                  WHERE e.conversation_id = m.conversation_id AND e.event_type = 'contacted'
              )
            ORDER BY m.conversation_id, m.created_at ASC
        """))
        logger.info("Seeded 'contacted' lead_events")

        # ── 9. Seed initial 'replied' events from first inbound messages ──────
        await conn.execute(text("""
            INSERT INTO lead_events (conversation_id, event_type, note, created_at)
            SELECT DISTINCT ON (m.conversation_id)
                m.conversation_id,
                'replied',
                'Backfilled from migration',
                m.created_at
            FROM whatsapp_messages m
            WHERE m.direction = 'inbound'
              AND NOT EXISTS (
                  SELECT 1 FROM lead_events e
                  WHERE e.conversation_id = m.conversation_id AND e.event_type = 'replied'
              )
            ORDER BY m.conversation_id, m.created_at ASC
        """))
        logger.info("Seeded 'replied' lead_events")

    await engine.dispose()
    logger.info("Migration complete.")


if __name__ == "__main__":
    asyncio.run(run_migration())
