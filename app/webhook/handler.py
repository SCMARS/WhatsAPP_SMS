import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, Conversation, WhatsAppMessage
from app.services.blacklist import add_to_blacklist, is_blacklisted, is_stop_message
from app.services.elevenlabs import generate_text_reply, get_agent_prompt, transcribe_audio
from app.services.sender import send_message

logger = logging.getLogger(__name__)


async def handle_incoming(
    db: AsyncSession,
    payload: dict[str, Any],
    instance_id: str,
) -> None:
    # 1. Filter: only incomingMessageReceived
    if payload.get("typeWebhook") != "incomingMessageReceived":
        return

    # Extract sender data
    sender_data = payload.get("senderData", {})
    chat_id = sender_data.get("chatId", "")

    # Skip group messages
    if "@g.us" in chat_id:
        return

    # 2. Extract phone, text, provider_message_id
    phone = chat_id.split("@")[0] if "@" in chat_id else chat_id
    if not phone:
        logger.warning("Could not extract phone from chatId")
        return

    message_data = payload.get("messageData", {})
    msg_type = message_data.get("typeMessage", "")
    provider_message_id: Optional[str] = payload.get("idMessage")

    # Map non-text message types to a placeholder text for the AI
    NON_TEXT_PLACEHOLDERS = {
        "imageMessage":    "[The customer sent a photo]",
        "videoMessage":    "[The customer sent a video]",
        "documentMessage": "[The customer sent a document/file]",
        "stickerMessage":  "[The customer sent a sticker]",
        "locationMessage": "[The customer sent their location]",
        "contactMessage":  "[The customer sent a contact card]",
    }

    if msg_type == "textMessage":
        text = message_data.get("textMessageData", {}).get("textMessage", "").strip()
        if not text:
            return

    elif msg_type == "audioMessage":
        # Transcribe voice message via ElevenLabs Scribe STT
        audio_url = message_data.get("fileMessageData", {}).get("downloadUrl", "")
        if not audio_url:
            logger.warning(f"audioMessage from {phone} has no downloadUrl, using placeholder")
            text = "[The customer sent a voice message]"
        else:
            transcribed = await transcribe_audio(audio_url)
            if transcribed:
                text = transcribed
                logger.info(f"Voice message from {phone} transcribed: {text[:80]}")
            else:
                text = "[The customer sent a voice message that could not be transcribed]"
                logger.warning(f"Failed to transcribe voice from {phone}")

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
            logger.debug(f"Duplicate message {provider_message_id}, skipping")
            return

    # 4. Blacklist check
    if await is_blacklisted(db, phone):
        logger.debug(f"Message from blacklisted phone {phone}, ignoring")
        return

    # 5. STOP check
    if is_stop_message(text):
        await add_to_blacklist(db, phone, reason="STOP request")
        logger.info(f"Phone {phone} sent STOP, added to blacklist")
        return

    # 6. Find most recent active Conversation by phone
    result = await db.execute(
        select(Conversation)
        .where(Conversation.phone == phone, Conversation.status == "active")
        .order_by(Conversation.created_at.desc())
        .limit(1)
    )
    conversation = result.scalar_one_or_none()

    if not conversation:
        logger.warning(f"No active conversation found for phone {phone}")
        return

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

    # 8. Load last 10 messages for history
    result = await db.execute(
        select(WhatsAppMessage)
        .where(WhatsAppMessage.conversation_id == conversation.id)
        .order_by(WhatsAppMessage.created_at.desc())
        .limit(10)
    )
    recent_messages = list(reversed(result.scalars().all()))

    # 9. Build llm_history
    llm_history = []
    for msg in recent_messages:
        role = "user" if msg.direction == "inbound" else "assistant"
        llm_history.append({"role": role, "content": msg.body})

    # 10. Get Campaign and agent_prompt cache
    result = await db.execute(
        select(Campaign).where(Campaign.id == conversation.campaign_id)
    )
    campaign = result.scalar_one_or_none()

    if not campaign:
        logger.error(f"Campaign not found for conversation {conversation.id}")
        return

    # 11. Fetch and cache agent_prompt if empty
    if not campaign.agent_prompt:
        try:
            agent_data = await get_agent_prompt(campaign.agent_id)
            campaign.agent_prompt = agent_data["prompt"]
            await db.commit()
        except Exception as e:
            logger.error(f"Failed to fetch agent prompt for campaign {campaign.id}: {e}")
            return

    # 12. Generate reply
    try:
        reply = await generate_text_reply(
            agent_id=campaign.agent_id,
            system_prompt=campaign.agent_prompt,
            history=llm_history,
            lead_name=conversation.lead_name,
        )
    except Exception as e:
        logger.error(f"ElevenLabs generate_text_reply failed: {e}")
        return

    if not reply:
        logger.warning(f"Empty reply from ElevenLabs for conversation {conversation.id}")
        return

    # 13. Send reply
    await send_message(
        db=db,
        conversation=conversation,
        text=reply,
        lead_name=conversation.lead_name,
        batch_index=0,
        is_reply=True,
    )
