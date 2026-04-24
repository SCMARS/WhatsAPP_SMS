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
import random
from typing import Optional

from sqlalchemy import select, update
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.db.models import TelegramInstance
from app.db.session import AsyncSessionLocal

# ---------------------------------------------------------------------------
# Per-account device fingerprint pool
# Each account is assigned one profile deterministically (by phone hash) so
# the fingerprint is stable across reconnects without storing it in DB first.
# Once assigned, it is persisted back to DB so it never changes.
# ---------------------------------------------------------------------------
_DEVICE_PROFILES = [
    {"device_model": "Samsung SM-S908E",   "system_version": "12",   "app_version": "10.3.1"},
    {"device_model": "Samsung SM-G998B",   "system_version": "11",   "app_version": "10.2.9"},
    {"device_model": "Samsung SM-A536B",   "system_version": "12",   "app_version": "10.3.5"},
    {"device_model": "Xiaomi 2201123G",    "system_version": "12",   "app_version": "10.3.1"},
    {"device_model": "Xiaomi 22071212AG",  "system_version": "12",   "app_version": "10.2.5"},
    {"device_model": "Redmi Note 11",      "system_version": "11",   "app_version": "10.1.5"},
    {"device_model": "OnePlus IN2013",     "system_version": "11",   "app_version": "10.1.1"},
    {"device_model": "OnePlus Nord 2T",    "system_version": "12",   "app_version": "10.3.1"},
    {"device_model": "Pixel 6a",           "system_version": "13",   "app_version": "10.3.5"},
    {"device_model": "Pixel 7",            "system_version": "13",   "app_version": "10.3.1"},
    {"device_model": "POCO X5 Pro",        "system_version": "12",   "app_version": "10.2.9"},
    {"device_model": "realme 9 Pro",       "system_version": "12",   "app_version": "10.3.1"},
    {"device_model": "OPPO Reno8",         "system_version": "12",   "app_version": "10.2.5"},
    {"device_model": "vivo V25",           "system_version": "12",   "app_version": "10.3.1"},
    {"device_model": "Motorola Edge 30",   "system_version": "12",   "app_version": "10.2.9"},
    {"device_model": "Nokia G60",          "system_version": "12",   "app_version": "10.1.5"},
    {"device_model": "Sony Xperia 10 IV", "system_version": "12",    "app_version": "10.3.1"},
    {"device_model": "Huawei Nova 9",      "system_version": "11",   "app_version": "10.2.5"},
    {"device_model": "ZTE Blade V40",      "system_version": "11",   "app_version": "10.1.1"},
    {"device_model": "Tecno Camon 19",     "system_version": "12",   "app_version": "10.2.9"},
]


def _pick_device_profile(phone_number: str) -> dict:
    """Deterministically pick a device profile based on the phone number."""
    idx = abs(hash(phone_number)) % len(_DEVICE_PROFILES)
    return _DEVICE_PROFILES[idx].copy()


def _build_proxy(inst: TelegramInstance) -> Optional[dict]:
    """Build proxy dict for TelegramClient if the instance has a proxy configured."""
    if not inst.proxy_host or not inst.proxy_port:
        return None
    try:
        import socks
        proxy: dict = {
            "proxy_type": socks.SOCKS5,
            "addr": inst.proxy_host,
            "port": inst.proxy_port,
            "rdns": True,
        }
        if inst.proxy_username:
            proxy["username"] = inst.proxy_username
            proxy["password"] = inst.proxy_password or ""
        return proxy
    except ImportError:
        logger.warning(
            "[TGClientManager] PySocks not installed — proxy ignored for %s. "
            "Run: pip install PySocks",
            inst.phone_number,
        )
        return None

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
        # Resolve device fingerprint — use stored values or auto-generate + persist
        if inst.device_model and inst.system_version and inst.app_version:
            device = {
                "device_model":   inst.device_model,
                "system_version": inst.system_version,
                "app_version":    inst.app_version,
                "lang_code":      inst.lang_code or "en",
                "system_lang_code": inst.lang_code or "en",
            }
        else:
            device = _pick_device_profile(inst.phone_number)
            device["lang_code"] = inst.lang_code or "en"
            device["system_lang_code"] = inst.lang_code or "en"
            # Persist so it never changes for this account
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(TelegramInstance)
                    .where(TelegramInstance.phone_number == inst.phone_number)
                    .values(
                        device_model=device["device_model"],
                        system_version=device["system_version"],
                        app_version=device["app_version"],
                        lang_code=device["lang_code"],
                    )
                )
                await db.commit()
            logger.info(
                "[TGClientManager] %s: assigned device fingerprint %s / Android %s",
                inst.phone_number, device["device_model"], device["system_version"],
            )

        proxy = _build_proxy(inst)

        session = StringSession(inst.session_string)
        client = TelegramClient(
            session,
            inst.api_id,
            inst.api_hash,
            device_model=device["device_model"],
            system_version=device["system_version"],
            app_version=device["app_version"],
            lang_code=device["lang_code"],
            system_lang_code=device["system_lang_code"],
            flood_sleep_threshold=20,  # auto-sleep for Telegram-requested waits ≤ 20s
            connection_retries=3,       # reconnect attempts before giving up
            retry_delay=5,              # seconds between reconnect attempts
            auto_reconnect=True,
            **({"proxy": proxy} if proxy else {}),
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
