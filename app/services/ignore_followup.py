import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import Campaign, Conversation, WhatsAppMessage
from app.db.session import AsyncSessionLocal
from app.services.elevenlabs import generate_text_reply
from app.services.sender import send_message

logger = logging.getLogger(__name__)

IGNORE_AFTER = timedelta(hours=3)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _process_conversation(conversation: Conversation) -> None:
    async with AsyncSessionLocal() as db:
        msgs_res = await db.execute(
            select(WhatsAppMessage)
            .where(WhatsAppMessage.conversation_id == conversation.id)
            .order_by(WhatsAppMessage.created_at.desc())
            .limit(30)
        )
        recent_desc = msgs_res.scalars().all()
        if not recent_desc:
            return

        latest = recent_desc[0]
        if latest.direction != "outbound":
            return
        if not latest.created_at or (_utc_now() - latest.created_at) < IGNORE_AFTER:
            return

        last_inbound = next((m for m in recent_desc if m.direction == "inbound"), None)
        last_ignore = next(
            (
                m
                for m in recent_desc
                if m.direction == "outbound"
                and isinstance(m.meta, dict)
                and m.meta.get("ignore_followup") is True
            ),
            None,
        )
        # If ignore follow-up already sent and user didn't answer after it, don't repeat.
        if last_ignore and (last_inbound is None or last_ignore.created_at > last_inbound.created_at):
            return

        camp_res = await db.execute(
            select(Campaign).where(Campaign.id == conversation.campaign_id)
        )
        campaign = camp_res.scalar_one_or_none()
        if not campaign:
            return

        recent = list(reversed(recent_desc))
        history = [
            {"role": "user" if m.direction == "inbound" else "assistant", "content": m.body}
            for m in recent
        ]
        # Signal to agent prompt: lead ignored previous message.
        history.append({"role": "user", "content": "игнор"})

        try:
            reply = await generate_text_reply(
                agent_id=campaign.agent_id,
                system_prompt=campaign.agent_prompt or "",
                history=history,
                lead_name=conversation.lead_name,
                chat_key=conversation.phone,
            )
        except Exception as e:
            logger.error(f"Ignore follow-up generation failed for {conversation.phone}: {e}")
            return

        reply = (reply or "").strip()
        if not reply:
            return

        msg = await send_message(
            db=db,
            conversation=conversation,
            text=reply,
            lead_name=conversation.lead_name,
            batch_index=0,
            is_reply=True,
        )
        if msg:
            msg.meta = {"ignore_followup": True}
            db.add(msg)
            await db.commit()
            logger.info(f"Ignore follow-up sent to {conversation.phone}")


async def process_ignore_followups_once() -> None:
    async with AsyncSessionLocal() as db:
        conv_res = await db.execute(
            select(Conversation).where(Conversation.status == "active")
        )
        conversations = conv_res.scalars().all()

    for conversation in conversations:
        try:
            await _process_conversation(conversation)
        except Exception as e:
            logger.error(f"Ignore follow-up processing error for {conversation.phone}: {e}")


async def run_ignore_followup_worker(stop_event: asyncio.Event) -> None:
    logger.info("Ignore follow-up worker started")
    while not stop_event.is_set():
        try:
            await process_ignore_followups_once()
        except Exception as e:
            logger.error(f"Ignore follow-up worker loop error: {e}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
    logger.info("Ignore follow-up worker stopped")
