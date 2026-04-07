import logging
import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Campaign, Conversation, WhatsAppInstance
from app.db.session import AsyncSessionLocal, get_db
from app.services.elevenlabs import generate_text_reply, get_agent_prompt
from app.services import pool as instance_pool
from app.services.blacklist import is_blacklisted
from app.services.sender import send_initial_message
from app.webhook.handler import handle_incoming

logger = logging.getLogger(__name__)

router = APIRouter()

async def _resolve_initial_message(
    campaign: Campaign,
    lead_name: Optional[str],
    phone: str,
    provided: Optional[str],
) -> str:
    # 1) If explicitly provided in payload/CSV -> use it.
    initial_text = (provided or "").strip()
    if initial_text:
        return initial_text

    # 2) Prefer ElevenLabs agent first_message.
    try:
        agent_data = await get_agent_prompt(campaign.agent_id)
    except Exception as e:
        logger.error(f"Failed to fetch agent first_message for campaign {campaign.id}: {e}")
        agent_data = {}

    initial_text = (agent_data.get("first_message") or "").strip()
    if initial_text:
        return initial_text

    # 3) If first_message is empty, ask ElevenLabs to generate opening line.
    try:
        generated = await generate_text_reply(
            agent_id=campaign.agent_id,
            system_prompt=campaign.agent_prompt or "",
            history=[{"role": "user", "content": "Сформулируй первое короткое приветственное сообщение клиенту."}],
            lead_name=lead_name,
            chat_key=phone,
        )
    except Exception as e:
        logger.error(f"Failed to generate initial message via ElevenLabs for {phone}: {e}")
        generated = ""
    generated = (generated or "").strip()
    if generated:
        return generated

    # 4) No hardcoded fallback: force proper ElevenLabs/campaign setup.
    raise HTTPException(
        status_code=422,
        detail="Cannot build initial message from ElevenLabs. Set agent first_message or provide initial_message.",
    )


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

async def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if x_api_key != settings.API_SECRET_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Request/Response schemas
# ---------------------------------------------------------------------------

class SendRequest(BaseModel):
    phone: str
    lead_id: str
    lead_name: Optional[str] = None
    campaign_external_id: str
    initial_message: Optional[str] = None
    batch_index: int = 0


class InstanceCreateRequest(BaseModel):
    name: str
    instance_id: str
    api_token: str
    phone_number: str
    daily_limit: int = 150
    hourly_limit: int = 30
    min_delay_sec: int = 8
    max_delay_sec: int = 25


class CampaignCreateRequest(BaseModel):
    external_id: str
    name: str
    agent_id: str


class BulkSendRequest(BaseModel):
    campaign_external_id: str
    leads: list[SendRequest]

class StopRequest(BaseModel):
    phone: str


class BulkStopRequest(BaseModel):
    phones: list[str]

class ResumeRequest(BaseModel):
    phone: str


class BulkResumeRequest(BaseModel):
    phones: list[str]

class ConfigAgentUpdateRequest(BaseModel):
    agent_id: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@router.post("/health")
async def health_check_post():
    # Some providers (e.g. Green API notifications) may probe/POST this endpoint.
    return {"status": "ok"}


async def _get_or_create_default_campaign(db: AsyncSession) -> Campaign:
    result = await db.execute(
        select(Campaign).where(Campaign.external_id == "default")
    )
    campaign = result.scalar_one_or_none()
    if campaign:
        return campaign

    campaign = Campaign(
        external_id="default",
        name="Default",
        agent_id=(settings.AGENT_ID or "").strip() or "default-agent",
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return campaign


@router.get("/api/config")
async def get_public_config(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    campaign = await _get_or_create_default_campaign(db)
    return {
        "api_key": settings.API_SECRET_KEY,
        "agent_id": campaign.agent_id,
    }


@router.post("/api/config/agent", dependencies=[Depends(require_api_key)])
async def update_default_agent(
    req: ConfigAgentUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    agent_id = (req.agent_id or "").strip()
    if not agent_id:
        raise HTTPException(status_code=422, detail="agent_id is required")

    campaign = await _get_or_create_default_campaign(db)
    campaign.agent_id = agent_id
    await db.commit()
    await db.refresh(campaign)
    return {"ok": True, "agent_id": campaign.agent_id}


@router.post("/api/send", dependencies=[Depends(require_api_key)])
async def send_message_endpoint(
    req: SendRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # 1. Blacklist check
    if await is_blacklisted(db, req.phone):
        return {"status": "blacklisted"}

    # 2. Find campaign
    result = await db.execute(
        select(Campaign).where(Campaign.external_id == req.campaign_external_id)
    )
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail=f"Campaign '{req.campaign_external_id}' not found")

    # 3. Find or create conversation
    result = await db.execute(
        select(Conversation).where(
            Conversation.campaign_id == campaign.id,
            Conversation.phone == req.phone,
        )
    )
    conversation = result.scalar_one_or_none()

    if conversation is None:
        conversation = Conversation(
            campaign_id=campaign.id,
            lead_id=req.lead_id,
            phone=req.phone,
            lead_name=req.lead_name,
            status="active",
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
    elif conversation.status == "stopped":
        return {"status": "skipped"}

    # 4. Determine initial message via ElevenLabs
    initial_text = await _resolve_initial_message(
        campaign=campaign,
        lead_name=req.lead_name,
        phone=req.phone,
        provided=req.initial_message,
    )

    # 5. Send initial message
    msg = await send_initial_message(
        db=db,
        conversation=conversation,
        initial_text=initial_text,
        batch_index=req.batch_index,
    )

    if msg is None:
        raise HTTPException(status_code=503, detail="No available WhatsApp instances")

    return {
        "status": "sent",
        "message_id": str(msg.id),
        "conversation_id": str(conversation.id),
    }


@router.post("/api/send/bulk", dependencies=[Depends(require_api_key)])
async def bulk_send_endpoint(
    req: BulkSendRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Send initial messages to up to 500 leads at once.
    Processes sequentially to respect instance rate limits and delays.
    Returns per-lead results.
    """
    results = []
    for lead in req.leads:
        # Override campaign_external_id from parent request if not set per-lead
        if not lead.campaign_external_id:
            lead.campaign_external_id = req.campaign_external_id

        # Blacklist check
        if await is_blacklisted(db, lead.phone):
            results.append({"phone": lead.phone, "status": "blacklisted"})
            continue

        # Find campaign
        result = await db.execute(
            select(Campaign).where(Campaign.external_id == lead.campaign_external_id)
        )
        campaign = result.scalar_one_or_none()
        if not campaign:
            results.append({"phone": lead.phone, "status": "error", "detail": "campaign not found"})
            continue

        # Find or create conversation
        result = await db.execute(
            select(Conversation).where(
                Conversation.campaign_id == campaign.id,
                Conversation.phone == lead.phone,
            )
        )
        conversation = result.scalar_one_or_none()

        if conversation is None:
            conversation = Conversation(
                campaign_id=campaign.id,
                lead_id=lead.lead_id,
                phone=lead.phone,
                lead_name=lead.lead_name,
                status="active",
            )
            db.add(conversation)
            await db.commit()
            await db.refresh(conversation)
        elif conversation.status == "stopped":
            results.append({"phone": lead.phone, "status": "skipped"})
            continue

        # Determine initial message via ElevenLabs
        try:
            initial_text = await _resolve_initial_message(
                campaign=campaign,
                lead_name=lead.lead_name,
                phone=lead.phone,
                provided=lead.initial_message,
            )
        except HTTPException as e:
            results.append({"phone": lead.phone, "status": "error", "detail": e.detail})
            continue
        except Exception as e:
            logger.error(f"Unexpected error while resolving initial message for {lead.phone}: {e}")
            results.append({"phone": lead.phone, "status": "error", "detail": "internal error while building initial message"})
            continue

        # Send
        msg = await send_initial_message(
            db=db,
            conversation=conversation,
            initial_text=initial_text,
            batch_index=lead.batch_index,
        )

        if msg is None:
            results.append({"phone": lead.phone, "status": "error", "detail": "no available instances"})
        elif msg.status not in ("sent", "queued"):
            results.append({"phone": lead.phone, "status": "error", "detail": msg.error or "send failed"})
        else:
            results.append({
                "phone": lead.phone,
                "status": "sent",
                "message_id": str(msg.id),
                "conversation_id": str(conversation.id),
            })

    sent = sum(1 for r in results if r["status"] == "sent")
    return {
        "total": len(results),
        "sent": sent,
        "results": results,
    }


def _phone_variants(phone: str) -> list[str]:
    p = (phone or "").strip()
    digits = "".join(c for c in p if c.isdigit())
    variants = {p}
    if digits:
        variants.add(digits)
        variants.add(f"+{digits}")
    return [v for v in variants if v]


@router.post("/api/stop", dependencies=[Depends(require_api_key)])
async def stop_one_endpoint(
    req: StopRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    variants = _phone_variants(req.phone)
    if not variants:
        raise HTTPException(status_code=422, detail="phone is required")

    result = await db.execute(
        update(Conversation)
        .where(Conversation.phone.in_(variants))
        .values(status="stopped")
    )
    await db.commit()

    updated = result.rowcount or 0
    return {"status": "stopped", "phone": req.phone, "updated": updated}


@router.post("/api/stop/bulk", dependencies=[Depends(require_api_key)])
async def stop_bulk_endpoint(
    req: BulkStopRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    total_updated = 0

    for phone in req.phones:
        variants = _phone_variants(phone)
        if not variants:
            results.append({"phone": phone, "status": "error", "detail": "phone is required"})
            continue

        r = await db.execute(
            update(Conversation)
            .where(Conversation.phone.in_(variants))
            .values(status="stopped")
        )
        updated = r.rowcount or 0
        total_updated += updated
        results.append({"phone": phone, "status": "stopped", "updated": updated})

    await db.commit()
    stopped = sum(1 for r in results if r["status"] == "stopped")
    return {"total": len(results), "stopped": stopped, "updated": total_updated, "results": results}


@router.post("/api/resume", dependencies=[Depends(require_api_key)])
async def resume_one_endpoint(
    req: ResumeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    variants = _phone_variants(req.phone)
    if not variants:
        raise HTTPException(status_code=422, detail="phone is required")

    result = await db.execute(
        update(Conversation)
        .where(Conversation.phone.in_(variants))
        .values(status="active")
    )
    await db.commit()

    updated = result.rowcount or 0
    return {"status": "active", "phone": req.phone, "updated": updated}


@router.post("/api/resume/bulk", dependencies=[Depends(require_api_key)])
async def resume_bulk_endpoint(
    req: BulkResumeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    total_updated = 0

    for phone in req.phones:
        variants = _phone_variants(phone)
        if not variants:
            results.append({"phone": phone, "status": "error", "detail": "phone is required"})
            continue

        r = await db.execute(
            update(Conversation)
            .where(Conversation.phone.in_(variants))
            .values(status="active")
        )
        updated = r.rowcount or 0
        total_updated += updated
        results.append({"phone": phone, "status": "active", "updated": updated})

    await db.commit()
    resumed = sum(1 for r in results if r["status"] == "active")
    return {"total": len(results), "resumed": resumed, "updated": total_updated, "results": results}


@router.post("/webhook/{instance_id}")
async def webhook_endpoint(
    instance_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    # Important: respond fast to Green API. Processing can take 10–30s (ElevenLabs),
    # so we do it asynchronously in a separate DB session.
    async def _run():
        async with AsyncSessionLocal() as session:
            try:
                await handle_incoming(session, payload, instance_id)
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.error(f"Webhook error: {e}", exc_info=True)

    asyncio.create_task(_run())
    return {"ok": True}


@router.get("/webhook/{instance_id}")
async def webhook_probe(instance_id: str) -> dict[str, Any]:
    # Some services probe webhook URLs with GET. Return 200 so they don't treat it as broken.
    return {"ok": True, "instance_id": instance_id}


@router.post("/api/instances", dependencies=[Depends(require_api_key)])
async def create_instance(
    req: InstanceCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # Check for duplicate
    result = await db.execute(
        select(WhatsAppInstance).where(WhatsAppInstance.instance_id == req.instance_id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Instance '{req.instance_id}' already exists")

    instance = WhatsAppInstance(
        name=req.name,
        instance_id=req.instance_id,
        api_token=req.api_token,
        phone_number=req.phone_number,
        daily_limit=req.daily_limit,
        hourly_limit=req.hourly_limit,
        min_delay_sec=req.min_delay_sec,
        max_delay_sec=req.max_delay_sec,
    )
    db.add(instance)
    await db.commit()
    await db.refresh(instance)

    return {
        "id": str(instance.id),
        "instance_id": instance.instance_id,
        "name": instance.name,
        "is_active": instance.is_active,
    }


@router.post("/api/campaigns", dependencies=[Depends(require_api_key)])
async def create_campaign(
    req: CampaignCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        select(Campaign).where(Campaign.external_id == req.external_id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Campaign '{req.external_id}' already exists")

    campaign = Campaign(
        external_id=req.external_id,
        name=req.name,
        agent_id=req.agent_id,
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)

    return {
        "id": str(campaign.id),
        "external_id": campaign.external_id,
        "name": campaign.name,
        "agent_id": campaign.agent_id,
    }


@router.get("/api/instances", dependencies=[Depends(require_api_key)])
async def list_instances(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(select(WhatsAppInstance))
    instances = result.scalars().all()
    stats = instance_pool.get_instance_stats()

    data = []
    for inst in instances:
        inst_stats = stats.get(inst.instance_id, {"hourly": 0, "daily": 0})
        data.append({
            "id": str(inst.id),
            "name": inst.name,
            "instance_id": inst.instance_id,
            "phone_number": inst.phone_number,
            "is_active": inst.is_active,
            "is_banned": inst.is_banned,
            "daily_limit": inst.daily_limit,
            "hourly_limit": inst.hourly_limit,
            "hourly_sent": inst_stats["hourly"],
            "daily_sent": inst_stats["daily"],
            "health_status": inst.health_status,
        })

    return {"instances": data, "total": len(data)}
