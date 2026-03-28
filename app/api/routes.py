import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Campaign, Conversation, WhatsAppInstance
from app.db.session import get_db
from app.services import pool as instance_pool
from app.services.blacklist import is_blacklisted
from app.services.sender import send_initial_message
from app.webhook.handler import handle_incoming

logger = logging.getLogger(__name__)

router = APIRouter()


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
    initial_message: str
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


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

    # 4. Send initial message
    msg = await send_initial_message(
        db=db,
        conversation=conversation,
        initial_text=req.initial_message,
        batch_index=req.batch_index,
    )

    if msg is None:
        raise HTTPException(status_code=503, detail="No available WhatsApp instances")

    return {
        "status": "sent",
        "message_id": str(msg.id),
        "conversation_id": str(conversation.id),
    }


@router.post("/webhook/{instance_id}")
async def webhook_endpoint(
    instance_id: str,
    payload: dict[str, Any],
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await handle_incoming(db, payload, instance_id)
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
    return {"ok": True}


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
