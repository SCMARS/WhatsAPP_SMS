"""
Quick Telegram auth — all params hardcoded, only OTP needed.

Usage:
    python3 telegram_auth_quick.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from telethon import TelegramClient
from telethon.sessions import StringSession
from sqlalchemy import select

from app.db.models import TelegramInstance
from app.db.session import AsyncSessionLocal

PHONE = "+380639458226"
API_ID = 31660552
API_HASH = "f5b2e7c0d0fcc335dfed0d77a9375e86"
NAME = "Account 1"
DAILY_LIMIT = 200
HOURLY_LIMIT = 20


async def authenticate() -> None:
    print(f"\nConnecting to Telegram as {PHONE}...")
    session = StringSession()
    client = TelegramClient(session, API_ID, API_HASH)

    await client.start(phone=PHONE)

    me = await client.get_me()
    print(f"\nAuthenticated as: {me.first_name} {me.last_name or ''} (@{me.username or 'no username'})")

    session_string = client.session.save()
    await client.disconnect()

    print("\nSaving session to database...")
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TelegramInstance).where(TelegramInstance.phone_number == PHONE)
        )
        inst = result.scalar_one_or_none()

        if inst:
            inst.session_string = session_string
            inst.api_id = API_ID
            inst.api_hash = API_HASH
            inst.name = NAME
            inst.is_authorized = True
            inst.is_active = True
            inst.health_status = "authorized"
            print(f"Updated existing instance: {inst.id}")
        else:
            inst = TelegramInstance(
                name=NAME,
                phone_number=PHONE,
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=session_string,
                is_authorized=True,
                is_active=True,
                health_status="authorized",
                daily_limit=DAILY_LIMIT,
                hourly_limit=HOURLY_LIMIT,
            )
            db.add(inst)
            print("Created new instance.")

        await db.commit()
        await db.refresh(inst)
        print(f"\nInstance ID : {inst.id}")
        print(f"Phone       : {inst.phone_number}")
        print(f"Name        : {inst.name}")
        print(f"Done! Restart the service to use this account.")


if __name__ == "__main__":
    asyncio.run(authenticate())
