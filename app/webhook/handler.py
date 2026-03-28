import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, Conversation, WhatsAppMessage
from app.services.blacklist import add_to_blacklist, is_blacklisted, is_stop_message
from app.services.elevenlabs import generate_text_reply, get_agent_prompt
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
    if message_data.get("typeMessage") != "textMessage":
        logger.debug(f"Skipping non-text message from {phone}")
        return

    text = message_data.get("textMessageData", {}).get("textMessage", "").strip()
    provider_message_id: Optional[str] = payload.get("idMessage")

    if not text:
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
    )
