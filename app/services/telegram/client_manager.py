"""
Telegram client manager — owns the in-memory pool of live TelegramClient objects.

All other Telegram services obtain a client via get_client().  The manager is a
module-level singleton so it can be imported anywhere without instantiation.

Startup (called from main.py lifespan):
    await startup_all_clients()

Shutdown (called from main.py lifespan):
    await shutdown_all_clients()
"""

import asyncio
import logging
from typing import Optional

from sqlalchemy import select, update
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.db.models import TelegramInstance
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# phone_number → TelegramClient (live, connected)
_clients: dict[str, TelegramClient] = {}
# phone_number → asyncio.Lock  (serialize sends on the same client)
_client_locks: dict[str, asyncio.Lock] = {}
# Guards modifications to _clients / _client_locks
_manager_lock = asyncio.Lock()
# Set to True after startup_all_clients() finishes so event handlers can proceed
_ready: bool = False

# Session persistence: re-save session string every N successful sends
_SAVE_SESSION_EVERY = 50
_send_counters: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_ready() -> bool:
    return _ready


def get_client(phone_number: str) -> Optional[TelegramClient]:
    """Return the live TelegramClient for this phone, or None."""
    return _clients.get(phone_number)


def get_send_lock(phone_number: str) -> asyncio.Lock:
    """Return a per-client lock to serialize sends from the same account."""
    lock = _client_locks.get(phone_number)
    if lock is None:
        lock = asyncio.Lock()
        _client_locks[phone_number] = lock
    return lock


async def startup_all_clients() -> None:
    """Connect all authorized, non-banned TelegramInstances. Called once at startup."""
    global _ready

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TelegramInstance).where(
                TelegramInstance.is_authorized == True,
                TelegramInstance.is_banned == False,
            )
        )
        instances = list(result.scalars().all())

    logger.info(f"[TGClientManager] Starting {len(instances)} Telegram client(s)…")

    for inst in instances:
        await _connect_instance(inst)

    _ready = True
    logger.info(f"[TGClientManager] Ready. Active clients: {list(_clients.keys())}")


async def shutdown_all_clients() -> None:
    """Disconnect all clients gracefully. Called on shutdown."""
    global _ready
    _ready = False

    phones = list(_clients.keys())
    for phone in phones:
        client = _clients.pop(phone, None)
        if client:
            try:
                await client.disconnect()
                logger.info(f"[TGClientManager] Disconnected {phone}")
            except Exception as e:
                logger.warning(f"[TGClientManager] Error disconnecting {phone}: {e}")
    _client_locks.clear()
    _send_counters.clear()


async def reconnect_client(phone_number: str) -> bool:
    """
    Re-fetch session from DB and reconnect.  Returns True if successful.
    Called by health_monitor when a client is found disconnected.
    """
    async with _manager_lock:
        # Disconnect old client if present
        old_client = _clients.pop(phone_number, None)
        if old_client:
            try:
                await old_client.disconnect()
            except Exception:
                pass

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(TelegramInstance).where(TelegramInstance.phone_number == phone_number)
            )
            inst = result.scalar_one_or_none()

        if inst is None or not inst.is_authorized or inst.is_banned:
            return False

        return await _connect_instance(inst)


async def disconnect_client(phone_number: str) -> None:
    """Remove and disconnect a single client (e.g. on ban)."""
    async with _manager_lock:
        client = _clients.pop(phone_number, None)
        _client_locks.pop(phone_number, None)
        _send_counters.pop(phone_number, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


async def maybe_save_session(phone_number: str) -> None:
    """Persist session string to DB every SAVE_SESSION_EVERY sends."""
    _send_counters[phone_number] = _send_counters.get(phone_number, 0) + 1
    if _send_counters[phone_number] % _SAVE_SESSION_EVERY != 0:
        return

    client = _clients.get(phone_number)
    if client is None:
        return

    try:
        new_session = client.session.save()
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(TelegramInstance)
                .where(TelegramInstance.phone_number == phone_number)
                .values(session_string=new_session)
            )
            await db.commit()
        logger.debug(f"[TGClientManager] Session saved for {phone_number}")
    except Exception as e:
        logger.warning(f"[TGClientManager] Could not save session for {phone_number}: {e}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _connect_instance(inst: TelegramInstance) -> bool:
    """
    Build client, connect, verify auth, register incoming handler.
    Returns True on success.
    """
    if not inst.session_string:
        logger.warning(f"[TGClientManager] {inst.phone_number}: no session string, skipping")
        return False

    try:
        session = StringSession(inst.session_string)
        client = TelegramClient(
            session,
            inst.api_id,
            inst.api_hash,
            flood_sleep_threshold=20,  # auto-sleep for waits ≤ 20s
        )
        await client.connect()

        if not await client.is_user_authorized():
            logger.warning(f"[TGClientManager] {inst.phone_number}: session expired")
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(TelegramInstance)
                    .where(TelegramInstance.phone_number == inst.phone_number)
                    .values(is_authorized=False, is_active=False, health_status="session_expired")
                )
                await db.commit()
            await client.disconnect()
            return False

        _clients[inst.phone_number] = client
        _client_locks.setdefault(inst.phone_number, asyncio.Lock())

        # Register incoming message handler
        from app.services.telegram.incoming import register_handlers
        register_handlers(client, inst)

        logger.info(f"[TGClientManager] Connected: {inst.phone_number} ({inst.name})")
        return True

    except Exception as e:
        logger.error(f"[TGClientManager] Failed to connect {inst.phone_number}: {e}")
        return False
