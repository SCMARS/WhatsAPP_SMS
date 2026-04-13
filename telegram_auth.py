"""
telegram_auth.py — One-time interactive auth for a new Telegram account.

Run this script ONCE per phone number to authenticate and store the Telethon
session string in the database.  After this the FastAPI service can use the
account without any further interaction.

Usage:
    python telegram_auth.py

Requirements:
    pip install telethon
    DATABASE_URL in .env or environment

How to get api_id / api_hash:
    1. Go to https://my.telegram.org/auth
    2. Log in → "API development tools"
    3. Create an app (name/platform don't matter)
    4. Copy api_id (integer) and api_hash (string)
"""

import asyncio
import logging
import os
import sys

# ── make project root importable ────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from telethon import TelegramClient
from telethon.sessions import StringSession
from sqlalchemy import select

from app.config import settings
from app.db.models import TelegramInstance
from app.db.session import AsyncSessionLocal

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def authenticate() -> None:
    print("\n=== Telegram Account Authentication ===\n")
    phone   = input("Phone number (international format, e.g. +380671234567): ").strip()
    api_id  = int(input("api_id (integer from my.telegram.org): ").strip())
    api_hash = input("api_hash (string from my.telegram.org): ").strip()
    name    = input("Instance name (e.g. 'Account 1'): ").strip() or phone

    daily_limit  = int(input("Daily message limit [default 200]: ").strip() or "200")
    hourly_limit = int(input("Hourly message limit [default 20]: ").strip() or "20")

    print("\nConnecting to Telegram…")
    session = StringSession()
    client = TelegramClient(session, api_id, api_hash)

    # client.start() handles:
    #   - sending the OTP code to phone
    #   - prompting for the code
    #   - prompting for 2FA password if enabled
    await client.start(phone=phone)

    me = await client.get_me()
    print(f"\nAuthenticated as: {me.first_name} {me.last_name or ''} (@{me.username or 'no username'})")

    session_string = client.session.save()
    await client.disconnect()

    print("\nSaving session to database…")
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TelegramInstance).where(TelegramInstance.phone_number == phone)
        )
        inst = result.scalar_one_or_none()

        if inst:
            inst.session_string = session_string
            inst.api_id         = api_id
            inst.api_hash       = api_hash
            inst.name           = name
            inst.is_authorized  = True
            inst.is_active      = True
            inst.health_status  = "authorized"
            print(f"Updated existing instance: {inst.id}")
        else:
            inst = TelegramInstance(
                name=name,
                phone_number=phone,
                api_id=api_id,
                api_hash=api_hash,
                session_string=session_string,
                is_authorized=True,
                is_active=True,
                health_status="authorized",
                daily_limit=daily_limit,
                hourly_limit=hourly_limit,
            )
            db.add(inst)
            print("Created new instance.")

        await db.commit()
        await db.refresh(inst)
        print(f"\nInstance ID : {inst.id}")
        print(f"Phone       : {inst.phone_number}")
        print(f"Name        : {inst.name}")
        print(f"Daily limit : {inst.daily_limit}")
        print(f"Hourly limit: {inst.hourly_limit}")
        print("\nAccount is ready. Restart the service to start using this account.")


if __name__ == "__main__":
    asyncio.run(authenticate())
