import logging
import asyncio
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Campaign, Conversation, WhatsAppMessage, Blacklist
from app.services.blacklist import add_to_blacklist, is_blacklisted, is_stop_message
from app.services.elevenlabs import generate_text_reply, get_agent_prompt, transcribe_audio
from app.services.gemini import describe_image
from app.services.sender import send_message, read_chat

logger = logging.getLogger(__name__)
_CHAT_LOCKS: dict[str, asyncio.Lock] = {}


def _get_chat_lock(chat_key: str) -> asyncio.Lock:
    lock = _CHAT_LOCKS.get(chat_key)
    if lock is None:
        lock = asyncio.Lock()
        _CHAT_LOCKS[chat_key] = lock
    return lock


async def handle_incoming(
    db: AsyncSession,
    payload: dict[str, Any],
    instance_id: str,
) -> None:
    # 1. Filter: only incomingMessageReceived
    type_webhook = payload.get("typeWebhook")
    if type_webhook != "incomingMessageReceived":
        logger.debug(f"Ignoring webhook type={type_webhook}")
        return

    # Extract sender data
    sender_data = payload.get("senderData", {})
    chat_id = sender_data.get("chatId", "")
    provider_message_id: Optional[str] = payload.get("idMessage")

    # Skip group messages
    if "@g.us" in chat_id:
        logger.info(f"Incoming group message ignored chatId={chat_id}")
        return

    # 2. Extract phone, text, provider_message_id
    phone = chat_id.split("@")[0] if "@" in chat_id else chat_id
    if not phone:
        logger.warning("Could not extract phone from chatId")
        return

    # Normalize phone so it matches records created via /api/send (which may include '+')
    digits = "".join(c for c in phone if c.isdigit())
    phone_variants = {phone}
    if digits:
        phone_variants.add(digits)
        phone_variants.add(f"+{digits}")
    chat_lock_key = digits or phone
    chat_lock = _get_chat_lock(chat_lock_key)

    async with chat_lock:
        await _handle_incoming_locked(
            db=db,
            payload=payload,
            instance_id=instance_id,
            phone=phone,
            digits=digits,
            phone_variants=phone_variants,
            provider_message_id=provider_message_id,
            chat_id=chat_id,
        )


async def _handle_incoming_locked(
    db: AsyncSession,
    payload: dict[str, Any],
    instance_id: str,
    phone: str,
    digits: str,
    phone_variants: set[str],
    provider_message_id: Optional[str],
    chat_id: str,
) -> None:

    message_data = payload.get("messageData", {})
    msg_type = message_data.get("typeMessage", "")
    logger.info(
        "Incoming webhook: instance_id=%s chatId=%s phone=%s digits=%s type=%s idMessage=%s",
        instance_id,
        chat_id,
        phone,
        digits,
        msg_type,
        provider_message_id,
    )

    NON_TEXT_PLACEHOLDERS = {
        "videoMessage":    "[The customer sent a video]",
        "documentMessage": "[The customer sent a document/file]",
        "stickerMessage":  "[The customer sent a sticker]",
        "locationMessage": "[The customer sent their location]",
        "contactMessage":  "[The customer sent a contact card]",
    }

    # Look up instance credentials once (needed for audio/image download fallback)
    from app.db.models import WhatsAppInstance
    inst_result = await db.execute(
        select(WhatsAppInstance).where(WhatsAppInstance.instance_id == instance_id)
    )
    wa_instance = inst_result.scalar_one_or_none()
    inst_api_token = wa_instance.api_token if wa_instance else None

    if msg_type == "textMessage":
        text = message_data.get("textMessageData", {}).get("textMessage", "").strip()
        if not text:
            return
        logger.info(f"Inbound text from {phone}: {text[:200]}")

    elif msg_type == "audioMessage":
        file_data = message_data.get("fileMessageData", {})
        audio_url = file_data.get("downloadUrl", "")

        if not audio_url and not inst_api_token:
            logger.warning(f"audioMessage from {phone} has no downloadUrl and no instance, using placeholder")
            text = "[The customer sent a voice message]"
        else:
            transcribed = await transcribe_audio(
                audio_url=audio_url,
                instance_id=instance_id,
                api_token=inst_api_token,
                message_id=provider_message_id,
            )
            if transcribed:
                text = transcribed
                logger.info(f"Voice message from {phone} transcribed: {text[:80]}")
            else:
                text = "[The customer sent a voice message that could not be transcribed]"
                logger.warning(f"Failed to transcribe voice from {phone}")

    elif msg_type == "imageMessage":
        file_data = message_data.get("fileMessageData", {})
        image_url = file_data.get("downloadUrl", "")
        caption = file_data.get("caption", "") or ""

        # If we can't analyze images (no Gemini key), reply immediately asking for text description.
        # This prevents the agent from "getting stuck" repeating the same photo disclaimer.
        if not settings.GEMINI_API_KEY:
            quick_reply = "Фото вижу как вложение, но не могу его анализировать. Опишите, пожалуйста, что на фото и что именно нужно."
            # We don't need LLM here; just respond and exit.
            result = await db.execute(
                select(Conversation)
                .where(Conversation.phone.in_(list(phone_variants)), Conversation.status == "active")
                .order_by(Conversation.created_at.desc())
                .limit(1)
            )
            conv = result.scalar_one_or_none()
            if conv:
                await send_message(db=db, conversation=conv, text=quick_reply, lead_name=conv.lead_name, batch_index=0, is_reply=True)
            return

        text = await describe_image(
            image_url=image_url,
            instance_id=instance_id,
            api_token=inst_api_token,
            message_id=provider_message_id,
            caption=caption if caption else None,
        )
        logger.info(f"Image from {phone} described: {text[:100]}")

    elif msg_type in NON_TEXT_PLACEHOLDERS:
        text = NON_TEXT_PLACEHOLDERS[msg_type]
        logger.info(f"Non-text message ({msg_type}) from {phone} — using placeholder")

    else:
        logger.debug(f"Unsupported message type '{msg_type}' from {phone}, ignoring")
        return

    # 3. Deduplicate by provider_message_id
    if provider_message_id:
        result = await db.execute(
            select(WhatsAppMessage).where(
                WhatsAppMessage.provider_message_id == provider_message_id
            )
        )
        if result.scalar_one_or_none():
            logger.info(f"Duplicate inbound message {provider_message_id}, skipping")
            return

    # 4. Blacklist check
    if await is_blacklisted(db, phone):
        logger.info(f"Message from blacklisted phone {phone}, ignoring")
        return

    # 5. STOP check
    if is_stop_message(text):
        await add_to_blacklist(db, phone, reason="STOP request")
        logger.info(f"Phone {phone} sent STOP, added to blacklist")
        # Send one-time unsubscribe confirmation (conversation may already be closed)
        conf_res = await db.execute(
            select(Conversation)
            .where(Conversation.phone.in_(list(phone_variants)))
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
        conf_conv = conf_res.scalar_one_or_none()
        if conf_conv:
            await send_message(
                db=db,
                conversation=conf_conv,
                text="Вас успішно відписано від розсилки. Більше ми вам не пишемо.",
                batch_index=0,
                is_reply=True,
            )
        return

    # 6. Find most recent active Conversation by phone
    result = await db.execute(
        select(Conversation)
        .where(Conversation.phone.in_(list(phone_variants)), Conversation.status == "active")
        .order_by(Conversation.created_at.desc())
        .limit(1)
    )
    conversation = result.scalar_one_or_none()

    if not conversation:
        # If user writes first (no prior /api/send), auto-create a conversation in default campaign.
        camp_res = await db.execute(
            select(Campaign).where(Campaign.external_id == "default")
        )
        campaign = camp_res.scalar_one_or_none()
        if not campaign:
            logger.warning(f"No active conversation found for phone {phone} and default campaign missing")
            return

        normalized_phone = f"+{digits}" if digits else phone
        conversation = Conversation(
            campaign_id=campaign.id,
            lead_id=normalized_phone or phone,
            phone=normalized_phone or phone,
            lead_name=None,
            status="active",
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
        logger.info(f"Auto-created conversation for inbound phone {conversation.phone}")

    # 6a. Mark as READ in WhatsApp to trigger E2EE key sync
    await read_chat(db, conversation)

    # 7. Save inbound message
    inbound_msg = WhatsAppMessage(
        conversation_id=conversation.id,
        direction="inbound",
        body=text,
        provider_message_id=provider_message_id,
        status="received",
    )
    db.add(inbound_msg)
    await db.commit()

    # 8. Ignore if user already replied (Anna logic: only 1 reply total)
    inbound_res = await db.execute(
        select(WhatsAppMessage).where(
            WhatsAppMessage.conversation_id == conversation.id,
            WhatsAppMessage.direction == "inbound"
        )
    )
    # The current message is already saved, so if count > 1, it's a second/third message.
    if len(inbound_res.scalars().all()) > 1:
        logger.info(f"Phone {phone} already replied. Ignoring subsequent messages as per Anna logic.")
        return

    # 9. Load last few messages for context
    result = await db.execute(
        select(WhatsAppMessage)
        .where(WhatsAppMessage.conversation_id == conversation.id)
        .order_by(WhatsAppMessage.created_at.desc())
        .limit(5)
    )
    recent_messages = list(reversed(result.scalars().all()))
    llm_history = [{"role": ("user" if m.direction == "inbound" else "assistant"), "content": m.body} for m in recent_messages]

    # 10. Get Campaign info
    result = await db.execute(select(Campaign).where(Campaign.id == conversation.campaign_id))
    campaign = result.scalar_one_or_none()
    if not campaign:
        logger.error(f"Campaign not found for conversation {conversation.id}")
        return

    # 11. Generate reply via ElevenLabs
    try:
        reply = await generate_text_reply(
            agent_id=campaign.agent_id,
            system_prompt=campaign.agent_prompt,
            history=llm_history,
            lead_name=conversation.lead_name,
            chat_key=conversation.phone,
        )
    except Exception as e:
        logger.error(f"ElevenLabs failed: {e}")
        reply = "Привет! По всем вопросам бонуса тебе лучше всего подскажут в нашем онлайн-чате на сайте. Заглядывай туда! 😉"

    if not reply:
        reply = "За подробностями по бонусу и условиям заходи к нам в чат поддержки на сайте, там помогут за секунду! ✨"

    # 12. Send reply
    await send_message(
        db=db,
        conversation=conversation,
        text=reply,
        lead_name=conversation.lead_name,
        batch_index=0,
        is_reply=True,
    )
