"""
Telegram incoming message handler — mirrors app/webhook/handler.py.

Instead of an HTTP webhook endpoint, Telethon fires NewMessage events on each
connected client.  register_handlers() is called for every client at startup.

Handler flow (same as WhatsApp):
  1. Skip non-private / bot messages
  2. Extract phone (may be None if privacy settings) → fallback to user_id lookup
  3. Per-chat lock to prevent race conditions
  4. Deduplicate by Telegram message_id
  5. Blacklist check
  6. STOP keyword check → blacklist + confirmation
  7. Find / auto-create active Conversation (platform='telegram')
  8. Save inbound TelegramMessage
  9. "Anna logic": only reply to the FIRST inbound message
  10. Load recent message history for ElevenLabs context
  11. Generate reply via ElevenLabs
  12. Send reply via send_tg_message
"""

import asyncio
import logging
from typing import Optional

from datetime import datetime, timezone
from sqlalchemy import func, select, update
from telethon import events, TelegramClient

from app.db.models import (
    Blacklist, Campaign, Conversation, LeadEvent, TelegramInstance, TelegramMessage,
)
from app.db.session import AsyncSessionLocal
from app.services.blacklist import add_to_blacklist, is_blacklisted, is_stop_message

logger = logging.getLogger(__name__)

# Per-chat locks: telegram_user_id (str) → asyncio.Lock
_CHAT_LOCKS: dict[str, asyncio.Lock] = {}


def _get_chat_lock(key: str) -> asyncio.Lock:
    lock = _CHAT_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _CHAT_LOCKS[key] = lock
    return lock


# ---------------------------------------------------------------------------
# Handler registration (called from client_manager.startup_all_clients)
# ---------------------------------------------------------------------------

def register_handlers(client: TelegramClient, inst: TelegramInstance) -> None:
    """Attach a NewMessage event handler to this client."""

    @client.on(events.NewMessage(incoming=True))
    async def on_new_message(event: events.NewMessage.Event) -> None:
        from app.services.telegram.client_manager import is_ready
        if not is_ready():
            return  # Ignore messages during startup

        if not event.is_private:
            return  # Skip group/channel messages

        sender = await event.get_sender()
        if sender is None or getattr(sender, "bot", False):
            return

        # Fire-and-forget to avoid blocking the Telethon event loop
        asyncio.create_task(
            _handle_tg_incoming(
                event=event,
                inst=inst,
                sender=sender,
            )
        )


# ---------------------------------------------------------------------------
# Core handler
# ---------------------------------------------------------------------------

async def _handle_tg_incoming(event, inst: TelegramInstance, sender) -> None:
    telegram_user_id: int = sender.id
    lock_key = str(telegram_user_id)
    chat_lock = _get_chat_lock(lock_key)

    async with chat_lock:
        await _handle_locked(event, inst, sender, telegram_user_id)


async def _handle_locked(event, inst: TelegramInstance, sender, telegram_user_id: int) -> None:
    # Extract text (Telethon: event.raw_text)
    text = (event.raw_text or "").strip()
    provider_message_id = str(event.message.id)

    # Phone from sender (may be None due to privacy settings)
    phone: Optional[str] = getattr(sender, "phone", None)
    if phone and not phone.startswith("+"):
        phone = f"+{phone}"

    logger.info(
        "[TGIncoming] inst=%s user_id=%s phone=%s msg_id=%s text=%.80s",
        inst.phone_number, telegram_user_id, phone, provider_message_id, text,
    )

    async with AsyncSessionLocal() as db:

        # 1. Resolve conversation (and phone) via DB if phone is unknown
        conversation: Optional[Conversation] = None
        if phone is None:
            # Look up via telegram_user_id from prior outbound messages
            msg_res = await db.execute(
                select(TelegramMessage)
                .where(
                    TelegramMessage.telegram_user_id == telegram_user_id,
                    TelegramMessage.direction == "outbound",
                )
                .order_by(TelegramMessage.created_at.desc())
                .limit(1)
            )
            prior_msg = msg_res.scalar_one_or_none()
            if prior_msg:
                conv_res = await db.execute(
                    select(Conversation).where(Conversation.id == prior_msg.conversation_id)
                )
                conversation = conv_res.scalar_one_or_none()
                if conversation:
                    phone = conversation.phone

        # 2. Deduplicate
        dup_res = await db.execute(
            select(TelegramMessage).where(
                TelegramMessage.provider_message_id == provider_message_id
            )
        )
        if dup_res.scalar_one_or_none():
            logger.debug(f"[TGIncoming] Duplicate message {provider_message_id}, skipping")
            return

        # 3. Blacklist check
        if phone and await is_blacklisted(db, phone):
            logger.info(f"[TGIncoming] Message from blacklisted {phone}, ignoring")
            return

        # 4. STOP keyword check
        if text and is_stop_message(text):
            if phone:
                await add_to_blacklist(db, phone, reason="STOP request")
                logger.info(f"[TGIncoming] {phone} sent STOP — blacklisted")
            if conversation:
                conversation.lead_status = "unsubscribed"
                conversation.is_blacklisted = True
                conversation.last_activity_at = datetime.now(timezone.utc)
                db.add(LeadEvent(
                    conversation_id=conversation.id,
                    event_type="unsubscribed",
                    note="STOP message received via Telegram",
                ))
                db.add(conversation)
                await db.commit()
                from app.services.telegram.sender import send_tg_message
                await send_tg_message(
                    db=db,
                    conversation=conversation,
                    text="Вас успішно відписано від розсилки. Більше ми вам не пишемо.",
                    batch_index=0,
                    is_reply=True,
                )
            return

        # 5. Find / auto-create active Conversation (platform='telegram')
        if conversation is None and phone:
            # Build phone variants (with/without '+') for robust lookup
            _digits = "".join(c for c in phone if c.isdigit())
            _phone_variants = list({phone, _digits, f"+{_digits}"} - {""})
            conv_res = await db.execute(
                select(Conversation).where(
                    Conversation.phone.in_(_phone_variants),
                    Conversation.platform == "telegram",
                    Conversation.status == "active",
                ).order_by(Conversation.created_at.desc()).limit(1)
            )
            conversation = conv_res.scalar_one_or_none()

        if conversation is None:
            # Auto-create from default campaign
            camp_res = await db.execute(
                select(Campaign).where(Campaign.external_id == "default")
            )
            campaign = camp_res.scalar_one_or_none()
            if not campaign:
                logger.warning(f"[TGIncoming] No active conversation for {phone} and no default campaign")
                return

            normalized = phone or f"tg_{telegram_user_id}"
            conversation = Conversation(
                campaign_id=campaign.id,
                lead_id=normalized,
                phone=normalized,
                lead_name=None,
                platform="telegram",
                status="active",
            )
            db.add(conversation)
            await db.commit()
            await db.refresh(conversation)
            logger.info(f"[TGIncoming] Auto-created conversation for {normalized}")

        # 6. Save inbound message
        inbound_msg = TelegramMessage(
            conversation_id=conversation.id,
            direction="inbound",
            body=text or "(empty)",
            provider_message_id=provider_message_id,
            telegram_user_id=telegram_user_id,
            status="received",
        )
        db.add(inbound_msg)
        await db.commit()

        # 6a. Update reply tracking (refresh first — object may be expired after commit)
        await db.refresh(conversation)
        now_utc = datetime.now(timezone.utc)
        is_first_reply = conversation.replied_at is None
        conversation.reply_count = (conversation.reply_count or 0) + 1
        conversation.last_activity_at = now_utc
        if is_first_reply:
            conversation.replied_at = now_utc
            if conversation.lead_status in ("new", "contacted", None):
                conversation.lead_status = "replied"
                db.add(LeadEvent(
                    conversation_id=conversation.id,
                    event_type="replied",
                    note=f"First Telegram reply: {(text or '')[:120]}",
                ))
        db.add(conversation)
        await db.commit()

        # 7. "Anna logic" — only reply to the FIRST inbound message per conversation
        inbound_count = (await db.execute(
            select(func.count()).where(
                TelegramMessage.conversation_id == conversation.id,
                TelegramMessage.direction == "inbound",
            )
        )).scalar_one()
        if inbound_count > 1:
            logger.info(f"[TGIncoming] {phone} already replied before — skipping AI reply")
            return

        # 8. Load recent history for ElevenLabs context
        hist_res = await db.execute(
            select(TelegramMessage)
            .where(TelegramMessage.conversation_id == conversation.id)
            .order_by(TelegramMessage.created_at.desc())
            .limit(5)
        )
        recent = list(reversed(hist_res.scalars().all()))
        llm_history = [
            {"role": "user" if m.direction == "inbound" else "assistant", "content": m.body}
            for m in recent
        ]

        # 9. Load campaign for agent_id
        camp_res = await db.execute(
            select(Campaign).where(Campaign.id == conversation.campaign_id)
        )
        campaign = camp_res.scalar_one_or_none()
        if not campaign:
            logger.error(f"[TGIncoming] Campaign not found for conversation {conversation.id}")
            return

        # 10. Generate reply via ElevenLabs
        from app.services.elevenlabs import generate_text_reply
        try:
            reply = await generate_text_reply(
                agent_id=campaign.agent_id,
                system_prompt=campaign.agent_prompt,
                history=llm_history,
                lead_name=conversation.lead_name,
                chat_key=conversation.phone,
                dynamic_variables={},
            )
        except Exception as e:
            logger.error(f"[TGIncoming] ElevenLabs failed: {e}")
            reply = "Привет! По всем вопросам заходи к нам в чат поддержки на сайте. 😊"

        if not reply:
            reply = "По деталям бонуса и условиям заходи к нам в чат поддержки на сайте! ✨"

        # 11. Send reply via the same instance that originally sent to this user
        from app.services.telegram.sender import send_tg_message

        # Prefer the instance that last sent to this conversation
        outbound_res = await db.execute(
            select(TelegramMessage)
            .where(
                TelegramMessage.conversation_id == conversation.id,
                TelegramMessage.direction == "outbound",
                TelegramMessage.instance_id.isnot(None),
            )
            .order_by(TelegramMessage.created_at.desc())
            .limit(1)
        )
        last_outbound = outbound_res.scalar_one_or_none()
        preferred_instance: Optional[TelegramInstance] = None
        if last_outbound and last_outbound.instance_id:
            inst_res = await db.execute(
                select(TelegramInstance).where(
                    TelegramInstance.id == last_outbound.instance_id,
                    TelegramInstance.is_active == True,
                )
            )
            preferred_instance = inst_res.scalar_one_or_none()

        await send_tg_message(
            db=db,
            conversation=conversation,
            text=reply,
            lead_name=conversation.lead_name,
            batch_index=0,
            is_reply=True,
            instance=preferred_instance,
        )
