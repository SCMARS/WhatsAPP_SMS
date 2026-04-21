"""
Telegram message sender — mirrors app/services/sender.py for Telegram.

Key flow:
  1. Get best TelegramInstance from pool
  2. Resolve phone → Telegram user_id via get_entity() (cached in DB + memory)
  3. Show typing indicator via client.action('typing')
  4. Send message
  5. Handle FloodWaitError (sleep + retry once), PeerFloodError, UserPrivacyRestrictedError,
     UserIsBlockedError, InputUserDeactivatedError, ChatWriteForbiddenError, AuthKeyError
  6. Persist TelegramMessage to DB
"""

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import (
    # Auth / session errors → mark instance session_expired or banned
    AuthKeyDuplicatedError,
    AuthKeyError,
    AuthKeyUnregisteredError,
    # Account-level bans → mark instance as permanently banned
    PhoneNumberBannedError,
    UserDeactivatedBanError,
    # Rate-limit errors → sleep + retry / cooldown
    FloodPremiumWaitError,
    FloodWaitError,
    PeerFloodError,
    # Recipient-side errors → blacklist lead (skip, not a ban for our account)
    ChatWriteForbiddenError,
    InputUserDeactivatedError,
    PeerIdInvalidError,
    UserBlockedError,
    UserDeactivatedError,
    UserIdInvalidError,
    UserIsBlockedError,
    UserNotMutualContactError,
    UserPrivacyRestrictedError,
)

from app.db.models import Conversation, TelegramInstance, TelegramMessage
from app.services.blacklist import add_to_blacklist
from app.services.telegram import pool as tg_pool
from app.services.telegram.client_manager import (
    get_client,
    get_send_lock,
    maybe_save_session,
)
from app.services.telegram.rate_limiter import (
    add_tg_footer,
    calc_typing_time,
    insert_zero_width,
    reply_pause,
    tg_wait_before_send,
)

logger = logging.getLogger(__name__)

# phone_digits → (telegram_user_id, cached_at) — module-level cache
_user_id_cache: dict[str, tuple[int, datetime]] = {}
_CACHE_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Phone → Telegram user_id resolution
# ---------------------------------------------------------------------------

def _phone_digits(phone: str) -> str:
    return "".join(c for c in phone if c.isdigit())


async def _resolve_user_id(
    db: AsyncSession,
    client,  # TelegramClient
    phone: str,
    instance: TelegramInstance,
) -> Optional[int]:
    """
    Resolve a phone number to a Telegram user_id.

    Strategy:
      1. Module-level cache (24h TTL)
      2. DB cache (last outbound TelegramMessage.telegram_user_id for this phone)
      3. import_contacts API call → delete contact immediately after resolution

    Returns None if the phone has no Telegram account (leads to blacklisting).
    """
    digits = _phone_digits(phone)
    now = datetime.now(timezone.utc)

    # 1. Memory cache
    cached = _user_id_cache.get(digits)
    if cached and (now - cached[1]).total_seconds() < _CACHE_TTL_HOURS * 3600:
        return cached[0]

    # 2. DB cache — look for the most recent outbound message to this phone
    result = await db.execute(
        select(TelegramMessage.telegram_user_id)
        .join(Conversation, TelegramMessage.conversation_id == Conversation.id)
        .where(
            Conversation.phone.in_([phone, f"+{digits}", digits]),
            TelegramMessage.direction == "outbound",
            TelegramMessage.telegram_user_id.isnot(None),
        )
        .order_by(TelegramMessage.created_at.desc())
        .limit(1)
    )
    uid_row = result.scalar_one_or_none()
    if uid_row is not None:
        _user_id_cache[digits] = (uid_row, now)
        return uid_row

    # 3. Direct entity resolution first
    try:
        entity = await client.get_entity(f"+{digits}")
        user_id = entity.id
        _user_id_cache[digits] = (user_id, now)
        logger.debug(f"[TGSender] Resolved +{digits} → user_id={user_id}")
        return user_id
    except (ValueError, UserIdInvalidError, PeerIdInvalidError):
        # Not in contacts — try ImportContacts
        pass
    except FloodWaitError as e:
        logger.warning(f"[TGSender] FloodWait {e.seconds}s during entity resolution for {phone}")
        raise
    except (UserDeactivatedError,):
        # Definitively no valid Telegram account at this number
        logger.info(f"[TGSender] Phone +{digits} has no Telegram account — blacklisting")
        await add_to_blacklist(db, phone, reason="tg_no_account")
        return None
    except Exception as e:
        # Transient error (network, DC migration, etc.) — do NOT blacklist
        logger.warning(f"[TGSender] Transient error resolving +{digits}: {type(e).__name__}: {e}")
        return None

    # 4. ImportContacts as fallback (add as contact, resolve, delete)
    try:
        from telethon.tl.types import InputPhoneContact
        from telethon.tl.functions.contacts import ImportContactsRequest

        contact = InputPhoneContact(client_id=0, phone=f"+{digits}", first_name="TempContact", last_name="")
        result = await client(ImportContactsRequest([contact]))

        if result.imported and len(result.users) > 0:
            user = result.users[0]
            user_id = user.id
            _user_id_cache[digits] = (user_id, now)
            logger.info(f"[TGSender] ImportContacts resolved +{digits} → user_id={user_id}")

            # Delete the temporary contact
            try:
                await client.delete_contacts(user_id)
            except Exception as e:
                logger.debug(f"[TGSender] Could not delete temp contact {digits}: {e}")

            return user_id
        else:
            logger.info(f"[TGSender] Phone +{digits} has no Telegram account — blacklisting")
            await add_to_blacklist(db, phone, reason="tg_no_account")
            return None

    except FloodWaitError as e:
        logger.warning(f"[TGSender] FloodWait {e.seconds}s during ImportContacts for {phone}")
        raise
    except Exception as e:
        logger.warning(f"[TGSender] ImportContacts failed for +{digits}: {type(e).__name__}: {e}")
        return None


async def _resolve_input_entity(
    client,  # TelegramClient
    phone: str,
    user_id: int,
):
    """
    Ensure the current Telethon client has a usable input entity for this user.

    A raw user_id cached in DB is not sufficient after client restart because the
    entity cache is empty. Falls back to resolving by phone string directly.
    Never uses ImportContactsRequest.
    """
    try:
        return await client.get_input_entity(user_id)
    except Exception:
        pass

    digits = _phone_digits(phone)
    try:
        return await client.get_input_entity(f"+{digits}")
    except Exception as e:
        logger.error(f"[TGSender] Could not hydrate input entity for {phone}: {e}")
        return None


# ---------------------------------------------------------------------------
# Core send function
# ---------------------------------------------------------------------------

async def send_tg_message(
    db: AsyncSession,
    conversation: Conversation,
    text: str,
    lead_name: Optional[str] = None,
    batch_index: int = 0,
    is_reply: bool = False,
    instance: Optional[TelegramInstance] = None,
) -> Optional[TelegramMessage]:

    if not instance:
        instance = await tg_pool.get_best_tg_instance(db)

    if not instance:
        logger.error("[TGSender] No available Telegram instances in pool")
        return None

    client = get_client(instance.phone_number)
    if client is None:
        logger.error(f"[TGSender] No live client for {instance.phone_number}")
        return None

    # Personalize
    personalized = insert_zero_width(text)
    if not is_reply:
        personalized = add_tg_footer(personalized)

    # Resolve user_id
    try:
        user_id = await _resolve_user_id(db, client, conversation.phone, instance)
    except FloodWaitError as e:
        await _handle_flood_wait(db, instance, e.seconds)
        return None

    if user_id is None:
        return None  # already blacklisted in _resolve_user_id

    peer = await _resolve_input_entity(client, conversation.phone, user_id)
    if peer is None:
        logger.error(f"[TGSender] Could not resolve input entity for {conversation.phone}")
        return None

    # Rate-limit delay
    if is_reply:
        await reply_pause()
    else:
        await tg_wait_before_send(instance)

    # Send (with per-client lock to prevent concurrent sends on same account)
    send_lock = get_send_lock(instance.phone_number)
    provider_message_id: Optional[str] = None
    error_text: Optional[str] = None
    status = "failed"

    async with send_lock:
        try:
            provider_message_id, error_text, status = await _do_tg_send(
                client, peer, personalized
            )
        except (FloodWaitError, FloodPremiumWaitError) as e:
            wait = e.seconds + random.uniform(5, 30)
            logger.warning(
                f"[TGSender] {'FloodPremium' if isinstance(e, FloodPremiumWaitError) else 'Flood'}"
                f"Wait {e.seconds}s on {instance.phone_number}, sleeping {wait:.0f}s"
            )
            await _handle_flood_wait(db, instance, e.seconds)
            await asyncio.sleep(wait)
            # One retry after the wait
            try:
                provider_message_id, error_text, status = await _do_tg_send(
                    client, peer, personalized
                )
            except Exception as e2:
                error_text = str(e2)
                logger.error(f"[TGSender] Retry failed for {conversation.phone}: {e2}")

        # ---- Recipient-side errors — skip this lead, NOT an account-level ban ----
        except (
            UserPrivacyRestrictedError,
            UserNotMutualContactError,
        ):
            logger.info(
                f"[TGSender] {conversation.phone} has privacy/mutual-contact restriction — blacklisting"
            )
            await add_to_blacklist(db, conversation.phone, reason="tg_privacy_restricted")
            return None
        except (
            UserIsBlockedError,
            UserBlockedError,
            InputUserDeactivatedError,
            UserDeactivatedError,
            PeerIdInvalidError,
            UserIdInvalidError,
        ):
            logger.info(f"[TGSender] {conversation.phone} is blocked/deactivated/invalid — blacklisting")
            await add_to_blacklist(db, conversation.phone, reason="tg_blocked")
            return None
        except ChatWriteForbiddenError:
            logger.info(f"[TGSender] ChatWriteForbidden for {conversation.phone} — blacklisting")
            await add_to_blacklist(db, conversation.phone, reason="tg_write_forbidden")
            return None

        # ---- Account-level rate flood — cooldown, not a ban ----
        except PeerFloodError:
            logger.warning(f"[TGSender] PeerFloodError on {instance.phone_number} — 30-min cooldown")
            await _handle_peer_flood(db, instance)
            return None

        # ---- Sender account permanently banned ----
        except (UserDeactivatedBanError, PhoneNumberBannedError) as e:
            logger.error(
                f"[TGSender] Account {instance.phone_number} is BANNED ({type(e).__name__}) — deactivating"
            )
            await tg_pool.mark_tg_banned(db, instance.phone_number)
            return None

        # ---- Auth / session errors ----
        except (AuthKeyUnregisteredError, AuthKeyDuplicatedError, AuthKeyError):
            logger.warning(f"[TGSender] Auth key invalid/expired for {instance.phone_number} — session expired")
            await _handle_session_expired(db, instance)
            return None

        except Exception as e:
            error_text = str(e)
            logger.error(f"[TGSender] Send failed for {conversation.phone}: {type(e).__name__}: {e}")

    if status == "sent":
        await tg_pool.record_tg_send(instance.phone_number)
        await maybe_save_session(instance.phone_number)

    msg = TelegramMessage(
        conversation_id=conversation.id,
        instance_id=instance.id,
        direction="outbound",
        body=personalized,
        provider_message_id=provider_message_id,
        telegram_user_id=user_id,
        status=status,
        error=error_text,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)

    logger.info(f"[TGSender] Message to {conversation.phone} status={status} id={msg.id}")
    return msg


async def _do_tg_send(
    client,
    peer,
    text: str,
) -> tuple[Optional[str], Optional[str], str]:
    """Show typing indicator then send. Returns (provider_id, error, status)."""
    typing_ms = calc_typing_time(text)
    typing_sec = typing_ms / 1000.0

    async with client.action(peer, "typing"):
        await asyncio.sleep(typing_sec)

    message = await client.send_message(peer, text)
    provider_id = str(message.id) if message else None
    return provider_id, None, "sent"


async def _handle_session_expired(
    db: AsyncSession,
    instance: TelegramInstance,
) -> None:
    """Session was revoked server-side; mark it expired and disconnect the client."""
    await db.execute(
        update(TelegramInstance)
        .where(TelegramInstance.phone_number == instance.phone_number)
        .values(is_authorized=False, is_active=False, health_status="session_expired")
    )
    await db.commit()
    from app.services.telegram.client_manager import disconnect_client
    await disconnect_client(instance.phone_number)
    logger.warning(f"[TGSender] {instance.phone_number} session expired — disconnected")


async def _handle_flood_wait(
    db: AsyncSession,
    instance: TelegramInstance,
    wait_seconds: int,
) -> None:
    """Increment flood_wait_count and temporarily deactivate the instance."""
    new_count = (instance.flood_wait_count or 0) + 1
    is_active = new_count < 5  # deactivate after 5 consecutive flood waits
    await db.execute(
        update(TelegramInstance)
        .where(TelegramInstance.phone_number == instance.phone_number)
        .values(
            flood_wait_count=new_count,
            is_active=is_active,
            health_status="flood_wait" if not is_active else instance.health_status,
        )
    )
    await db.commit()


async def _handle_peer_flood(
    db: AsyncSession,
    instance: TelegramInstance,
) -> None:
    """
    PeerFloodError = account temporarily restricted for bulk messaging.
    Deactivate for 30 minutes then re-enable.
    """
    await db.execute(
        update(TelegramInstance)
        .where(TelegramInstance.phone_number == instance.phone_number)
        .values(is_active=False, health_status="peer_flood")
    )
    await db.commit()

    phone = instance.phone_number

    async def _reactivate() -> None:
        await asyncio.sleep(30 * 60)  # 30-minute cooldown
        async with __import__("app.db.session", fromlist=["AsyncSessionLocal"]).AsyncSessionLocal() as db2:
            await db2.execute(
                update(TelegramInstance)
                .where(TelegramInstance.phone_number == phone)
                .values(is_active=True, health_status="authorized", flood_wait_count=0)
            )
            await db2.commit()
        logger.info(f"[TGSender] {phone} reactivated after PeerFlood cooldown")

    asyncio.create_task(_reactivate())


# ---------------------------------------------------------------------------
# Multi-part initial message (mirrors send_initial_message)
# ---------------------------------------------------------------------------

async def send_initial_tg_message(
    db: AsyncSession,
    conversation: Conversation,
    initial_text: str | list[str],
    batch_index: int = 0,
) -> Optional[TelegramMessage]:
    messages = [initial_text] if isinstance(initial_text, str) else initial_text
    last_msg: Optional[TelegramMessage] = None
    selected_instance: Optional[TelegramInstance] = None

    for i, text in enumerate(messages):
        is_seq = i > 0
        msg = await send_tg_message(
            db=db,
            conversation=conversation,
            text=text,
            lead_name=conversation.lead_name,
            batch_index=batch_index,
            is_reply=is_seq,
            instance=selected_instance,
        )
        if i == 0:
            last_msg = msg
            if msg and msg.instance_id:
                res = await db.execute(
                    select(TelegramInstance).where(TelegramInstance.id == msg.instance_id)
                )
                selected_instance = res.scalar_one_or_none()

        # Track outbound funnel stats (mirrors sender.py logic for WhatsApp)
        if msg and msg.status in ("sent", "queued"):
            await db.refresh(conversation)
            now_utc = datetime.now(timezone.utc)
            conversation.outbound_count = (conversation.outbound_count or 0) + 1
            conversation.last_activity_at = now_utc
            if conversation.first_contact_at is None and i == 0:
                conversation.first_contact_at = now_utc
            if conversation.lead_status in ("new", None):
                conversation.lead_status = "contacted"
                if i == 0:
                    from app.db.models import LeadEvent
                    db.add(LeadEvent(
                        conversation_id=conversation.id,
                        event_type="contacted",
                        note="Initial Telegram outreach sent",
                    ))
            db.add(conversation)
            await db.commit()

        if i < len(messages) - 1:
            await reply_pause(min_sec=5.0, max_sec=12.0)

    return last_msg
