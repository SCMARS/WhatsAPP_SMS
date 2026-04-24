"""
Telegram health monitor — polls every 120s, checks all active TelegramInstances.

Unlike WhatsApp (where Green API provides a state string), Telegram health is
checked by making a lightweight API call (get_me) and observing errors.

States mapped:
  get_me() succeeds             → authorized
  AuthKeyUnregisteredError      → session_expired (needs re-auth via telegram_auth.py)
  UserDeactivatedError          → deactivated / banned
  FloodWaitError during poll    → flood_wait (temporary, auto-recovers)
  client is None / disconnected → attempt reconnect
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TelegramInstance
from app.db.session import AsyncSessionLocal

# @SpamBot check: run at most once per 24 h per account
_SPAMBOT_CHECK_INTERVAL_HOURS = 24
# Phrases in @SpamBot response that indicate the account is clean
_SPAMBOT_CLEAN_PHRASES = (
    "no limits are currently",
    "no longer limited",
    "not limited",
)
# Phrases that indicate the account is restricted
_SPAMBOT_RESTRICTED_PHRASES = (
    "limited because",
    "been limited",
    "restricted",
    "can't send",
)
from app.services.telegram.reply_monitor import (
    TG_BLOCK_RATE_DANGER,
    TG_BLOCK_RATE_WARNING,
    TG_REPLY_RATE_DANGER,
    TG_REPLY_RATE_WARNING,
    get_tg_block_rate,
    get_tg_reply_rate,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 120


async def run_tg_health_monitor(stop_event: asyncio.Event) -> None:
    """Background task: poll all instances every POLL_INTERVAL_SECONDS."""
    logger.info("[TGHealthMonitor] Started (interval=%ds)", POLL_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            await _run_cycle()
        except Exception as e:
            logger.error(f"[TGHealthMonitor] Cycle error: {e}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _run_cycle() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TelegramInstance).where(TelegramInstance.is_banned == False)
        )
        instances = list(result.scalars().all())

    for inst in instances:
        try:
            await check_tg_instance(inst)
        except Exception as e:
            logger.error(f"[TGHealthMonitor] Error checking {inst.phone_number}: {e}")


async def check_tg_instance(inst: TelegramInstance) -> None:
    """Health-check one TelegramInstance and update its status in DB."""
    from app.services.telegram.client_manager import get_client, reconnect_client
    from telethon.errors import (
        AuthKeyUnregisteredError,
        AuthKeyDuplicatedError,
        FloodWaitError,
        UserDeactivatedError,
        UserDeactivatedBanError,
    )

    phone = inst.phone_number
    client = get_client(phone)

    if client is None or not client.is_connected():
        logger.warning(f"[TGHealthMonitor] {phone}: client missing/disconnected — attempting reconnect")
        reconnected = await reconnect_client(phone)
        if not reconnected:
            await _update_status(phone, "disconnected", is_active=False)
            return
        client = get_client(phone)
        if client is None:
            return

    try:
        me = await client.get_me()
        if me is None:
            await _update_status(phone, "session_expired", is_active=False)
            return
    except (AuthKeyUnregisteredError, AuthKeyDuplicatedError):
        logger.warning(f"[TGHealthMonitor] {phone}: session expired/revoked")
        await _update_status(phone, "session_expired", is_active=False, is_authorized=False)
        return
    except (UserDeactivatedError, UserDeactivatedBanError):
        logger.error(f"[TGHealthMonitor] {phone}: account deactivated/banned by Telegram")
        await _update_status(phone, "deactivated", is_active=False, is_banned=True)
        from app.services.telegram.client_manager import disconnect_client
        await disconnect_client(phone)
        return
    except FloodWaitError as e:
        logger.warning(f"[TGHealthMonitor] {phone}: FloodWait {e.seconds}s during health check")
        await _update_status(phone, "flood_wait", is_active=False)
        # Schedule re-activation after wait
        async def _reactivate(p: str, wait: int) -> None:
            await asyncio.sleep(wait + 30)
            await _update_status(p, "authorized", is_active=True, reset_flood=True)
        asyncio.create_task(_reactivate(phone, e.seconds))
        return
    except Exception as e:
        logger.warning(f"[TGHealthMonitor] {phone}: unexpected error {type(e).__name__}: {e}")
        return

    # Healthy — reset flood count, update status
    await _update_status(phone, "authorized", is_active=True, reset_flood=True)

    # @SpamBot proactive check (once per 24 h)
    await _maybe_check_spambot(client, inst)

    # Check reply and block rates
    async with AsyncSessionLocal() as db:
        rr = await get_tg_reply_rate(db, phone)
        br = await get_tg_block_rate(db, phone)

    if rr is not None:
        if rr < TG_REPLY_RATE_DANGER:
            logger.error("[TGHealthMonitor] %s reply rate=%.1f%% — DANGER (below %.0f%%)",
                         phone, rr * 100, TG_REPLY_RATE_DANGER * 100)
        elif rr < TG_REPLY_RATE_WARNING:
            logger.warning("[TGHealthMonitor] %s reply rate=%.1f%% — WARNING", phone, rr * 100)

    if br is not None:
        if br > TG_BLOCK_RATE_DANGER:
            logger.error("[TGHealthMonitor] %s block rate=%.1f%% — DANGER (above %.0f%%)",
                         phone, br * 100, TG_BLOCK_RATE_DANGER * 100)
        elif br > TG_BLOCK_RATE_WARNING:
            logger.warning("[TGHealthMonitor] %s block rate=%.1f%% — WARNING", phone, br * 100)


async def _maybe_check_spambot(client, inst: TelegramInstance) -> None:
    """
    Send /start to @SpamBot and parse its reply to detect soft-bans.

    Only runs once per _SPAMBOT_CHECK_INTERVAL_HOURS per account.
    A soft-ban (account limited but not session_expired) is logged as a warning
    and reflected in the spambot_ok field — the account stays active so operators
    can decide whether to pause it.
    """
    now = datetime.now(timezone.utc)
    if inst.spambot_checked_at is not None:
        checked_at = inst.spambot_checked_at
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        if (now - checked_at).total_seconds() < _SPAMBOT_CHECK_INTERVAL_HOURS * 3600:
            return  # checked recently, skip

    try:
        await client.send_message("spambot", "/start")
        await asyncio.sleep(3)  # wait for @SpamBot to reply

        messages = await client.get_messages("spambot", limit=1)
        if not messages:
            return

        response_text = (messages[0].message or "").lower()

        if any(phrase in response_text for phrase in _SPAMBOT_CLEAN_PHRASES):
            spambot_ok = True
            logger.info("[TGHealthMonitor] %s @SpamBot: account clean ✓", inst.phone_number)
        elif any(phrase in response_text for phrase in _SPAMBOT_RESTRICTED_PHRASES):
            spambot_ok = False
            logger.warning(
                "[TGHealthMonitor] %s @SpamBot: account RESTRICTED — reduce send volume. "
                "Response: %s",
                inst.phone_number, response_text[:200],
            )
        else:
            spambot_ok = None  # unknown response
            logger.debug("[TGHealthMonitor] %s @SpamBot: unrecognised response: %s",
                         inst.phone_number, response_text[:200])

        async with AsyncSessionLocal() as db:
            await db.execute(
                update(TelegramInstance)
                .where(TelegramInstance.phone_number == inst.phone_number)
                .values(spambot_ok=spambot_ok, spambot_checked_at=now)
            )
            await db.commit()

    except Exception as e:
        logger.warning("[TGHealthMonitor] %s @SpamBot check failed: %s", inst.phone_number, e)


async def _update_status(
    phone_number: str,
    health_status: str,
    is_active: bool = True,
    is_authorized: Optional[bool] = None,
    is_banned: bool = False,
    reset_flood: bool = False,
) -> None:
    values: dict = {
        "health_status": health_status,
        "is_active": is_active,
        "last_health_check": datetime.now(timezone.utc),
    }
    if is_authorized is not None:
        values["is_authorized"] = is_authorized
    if is_banned:
        values["is_banned"] = True
    if reset_flood:
        values["flood_wait_count"] = 0

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(TelegramInstance)
            .where(TelegramInstance.phone_number == phone_number)
            .values(**values)
        )
        await db.commit()
