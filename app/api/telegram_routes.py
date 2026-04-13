"""
Telegram REST API routes — mirrors app/api/routes.py for Telegram.

All routes are prefixed /api/telegram/ and protected by the same API key.

Reuses:
  - Campaign, Conversation, Blacklist, LinkPool models (shared with WhatsApp)
  - _resolve_initial_message, _split_outreach_into_three_random_parts from routes.py
  - ElevenLabs message generation
  - country / link pool detection
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Campaign, Conversation, TelegramInstance, TelegramMessage
from app.db.session import AsyncSessionLocal, get_db
from app.services.blacklist import is_blacklisted
from app.services.country import detect_country
from app.services.link_pool import claim_link
from app.services.rate_limiter import batch_pause
from app.services.telegram import pool as tg_pool
from app.services.telegram.client_manager import (
    get_client,
    reconnect_client,
    startup_all_clients as _connect_single,
)
from app.services.telegram.pool import (
    get_tg_instance_stats,
    get_tg_warmup_status,
)
from app.services.telegram.reply_monitor import (
    classify_tg_block_rate,
    classify_tg_reply_rate,
    get_tg_block_rate,
    get_tg_reply_rate,
    TG_BLOCK_RATE_DANGER,
    TG_BLOCK_RATE_WARNING,
    TG_REPLY_RATE_DANGER,
    TG_REPLY_RATE_WARNING,
)
from app.services.telegram.sender import send_initial_tg_message

# Re-use helper functions from the WA routes module
from app.api.routes import (
    _resolve_initial_message,
    _split_outreach_into_three_random_parts,
    require_api_key,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class TGSendRequest(BaseModel):
    phone: str
    lead_id: Optional[str] = None
    lead_name: Optional[str] = None
    campaign_external_id: Optional[str] = None
    initial_message: Optional[str] = None
    batch_index: int = 0


class TGBulkSendRequest(BaseModel):
    campaign_external_id: str
    leads: list[TGSendRequest]


class TGStopRequest(BaseModel):
    phone: str


class TGBulkStopRequest(BaseModel):
    phones: list[str]


class TGResumeRequest(BaseModel):
    phone: str


class TGBulkResumeRequest(BaseModel):
    phones: list[str]


class TGInstanceCreateRequest(BaseModel):
    name: str
    phone_number: str
    api_id: int
    api_hash: str
    daily_limit: int = 200
    hourly_limit: int = 20
    min_delay_sec: int = 10
    max_delay_sec: int = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _phone_variants(phone: str) -> list[str]:
    p = (phone or "").strip()
    digits = "".join(c for c in p if c.isdigit())
    variants = {p}
    if digits:
        variants.add(digits)
        variants.add(f"+{digits}")
    return list(variants)


async def _find_or_create_tg_conversation(
    db: AsyncSession,
    campaign: Campaign,
    phone: str,
    lead_id: Optional[str],
    lead_name: Optional[str],
) -> Conversation:
    result = await db.execute(
        select(Conversation).where(
            Conversation.campaign_id == campaign.id,
            Conversation.phone == phone,
            Conversation.platform == "telegram",
        )
    )
    conversation = result.scalar_one_or_none()
    if conversation is None:
        conversation = Conversation(
            campaign_id=campaign.id,
            lead_id=lead_id or phone,
            phone=phone,
            lead_name=lead_name,
            platform="telegram",
            status="active",
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
    return conversation


# ---------------------------------------------------------------------------
# Send endpoints
# ---------------------------------------------------------------------------

@router.post("/api/telegram/send", dependencies=[Depends(require_api_key)])
async def tg_send_endpoint(
    req: TGSendRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # Country / campaign resolution
    country_info = detect_country(req.phone)
    country_code = country_info["code"]
    lang = country_info["lang"]
    promo = country_info["promo"]
    campaign_key = req.campaign_external_id or country_info["campaign"]
    lead_id = req.lead_id or req.phone

    # Blacklist check
    for p in _phone_variants(req.phone):
        if await is_blacklisted(db, p):
            return {"status": "blacklisted"}

    # Claim link
    if country_code not in ("PT", "AR"):
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported country: {country_code}",
        )
    link_url = await claim_link(db, country_code, lead_id)
    if link_url is None:
        raise HTTPException(
            status_code=503,
            detail=f"Link pool exhausted for country={country_code}",
        )

    # Campaign
    result = await db.execute(select(Campaign).where(Campaign.external_id == campaign_key))
    campaign = result.scalar_one_or_none()
    if not campaign:
        campaign = Campaign(
            external_id=campaign_key,
            name=country_info["name"],
            agent_id=(settings.AGENT_ID or "").strip() or "default-agent",
        )
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)

    # Conversation
    conversation = await _find_or_create_tg_conversation(db, campaign, req.phone, lead_id, req.lead_name)
    if conversation.status == "stopped":
        return {"status": "skipped"}

    # Generate message
    initial_text = await _resolve_initial_message(
        db=db,
        campaign=campaign,
        lead_name=req.lead_name,
        phone=req.phone,
        provided=req.initial_message,
        language=lang,
        link_url=link_url,
        promo_code=promo,
    )

    async def bg_tg_send(conv_id, texts: list[str], batch_idx: int) -> None:
        async with AsyncSessionLocal() as bg_db:
            res = await bg_db.execute(select(Conversation).where(Conversation.id == conv_id))
            conv = res.scalar_one_or_none()
            if conv:
                await send_initial_tg_message(bg_db, conv, texts, batch_idx)

    background_tasks.add_task(bg_tg_send, conversation.id, initial_text, req.batch_index)

    return {
        "status": "queued",
        "conversation_id": str(conversation.id),
        "country": country_code,
        "lang": lang,
        "link_url": link_url,
    }


@router.post("/api/telegram/send/bulk", dependencies=[Depends(require_api_key)])
async def tg_bulk_send_endpoint(
    req: TGBulkSendRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    results = []
    for i, lead in enumerate(req.leads):
        if i > 0 and i % 10 == 0:
            await batch_pause(i)

        if not lead.campaign_external_id:
            lead.campaign_external_id = req.campaign_external_id

        # Blacklist check
        blacklisted = False
        for p in _phone_variants(lead.phone):
            if await is_blacklisted(db, p):
                blacklisted = True
                break
        if blacklisted:
            results.append({"phone": lead.phone, "status": "blacklisted"})
            continue

        # Campaign
        result = await db.execute(
            select(Campaign).where(Campaign.external_id == lead.campaign_external_id)
        )
        campaign = result.scalar_one_or_none()
        if not campaign:
            results.append({"phone": lead.phone, "status": "error", "detail": "campaign not found"})
            continue

        # Conversation
        conversation = await _find_or_create_tg_conversation(
            db, campaign, lead.phone, lead.lead_id, lead.lead_name
        )
        if conversation.status == "stopped":
            results.append({"phone": lead.phone, "status": "skipped"})
            continue

        # Country / link
        country_info = detect_country(lead.phone)
        country_code = country_info["code"]
        lang = country_info["lang"]
        promo = country_info["promo"]

        if country_code not in ("PT", "AR"):
            results.append({"phone": lead.phone, "status": "error", "detail": f"unsupported country: {country_code}"})
            continue

        link_url = await claim_link(db, country_code, lead.lead_id or lead.phone)
        if link_url is None:
            results.append({"phone": lead.phone, "status": "error", "detail": f"link pool exhausted for {country_code}"})
            continue

        try:
            initial_text = await _resolve_initial_message(
                db=db,
                campaign=campaign,
                lead_name=lead.lead_name,
                phone=lead.phone,
                provided=lead.initial_message,
                language=lang,
                link_url=link_url,
                promo_code=promo,
            )
        except HTTPException as e:
            results.append({"phone": lead.phone, "status": "error", "detail": e.detail})
            continue
        except Exception as e:
            logger.error(f"[TGRoutes] Message generation failed for {lead.phone}: {e}")
            results.append({"phone": lead.phone, "status": "error", "detail": "message generation failed"})
            continue

        msg = await send_initial_tg_message(
            db=db,
            conversation=conversation,
            initial_text=initial_text,
            batch_index=i,
        )

        if msg is None:
            results.append({"phone": lead.phone, "status": "error", "detail": "no available instances or send failed"})
        elif msg.status != "sent":
            results.append({"phone": lead.phone, "status": "error", "detail": msg.error or "send failed"})
        else:
            results.append({
                "phone": lead.phone,
                "status": "sent",
                "message_id": str(msg.id),
                "conversation_id": str(conversation.id),
            })

    sent = sum(1 for r in results if r["status"] == "sent")
    return {"total": len(results), "sent": sent, "results": results}


# ---------------------------------------------------------------------------
# Stop / Resume
# ---------------------------------------------------------------------------

@router.post("/api/telegram/stop", dependencies=[Depends(require_api_key)])
async def tg_stop_endpoint(
    req: TGStopRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    variants = _phone_variants(req.phone)
    await db.execute(
        update(Conversation)
        .where(
            Conversation.phone.in_(variants),
            Conversation.platform == "telegram",
            Conversation.status == "active",
        )
        .values(status="stopped")
    )
    await db.commit()
    return {"ok": True}


@router.post("/api/telegram/stop/bulk", dependencies=[Depends(require_api_key)])
async def tg_bulk_stop_endpoint(
    req: TGBulkStopRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    all_variants = []
    for phone in req.phones:
        all_variants.extend(_phone_variants(phone))
    await db.execute(
        update(Conversation)
        .where(
            Conversation.phone.in_(all_variants),
            Conversation.platform == "telegram",
            Conversation.status == "active",
        )
        .values(status="stopped")
    )
    await db.commit()
    return {"ok": True, "stopped": len(req.phones)}


@router.post("/api/telegram/resume", dependencies=[Depends(require_api_key)])
async def tg_resume_endpoint(
    req: TGResumeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    variants = _phone_variants(req.phone)
    await db.execute(
        update(Conversation)
        .where(
            Conversation.phone.in_(variants),
            Conversation.platform == "telegram",
            Conversation.status == "stopped",
        )
        .values(status="active")
    )
    await db.commit()
    return {"ok": True}


@router.post("/api/telegram/resume/bulk", dependencies=[Depends(require_api_key)])
async def tg_bulk_resume_endpoint(
    req: TGBulkResumeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    all_variants = []
    for phone in req.phones:
        all_variants.extend(_phone_variants(phone))
    await db.execute(
        update(Conversation)
        .where(
            Conversation.phone.in_(all_variants),
            Conversation.platform == "telegram",
            Conversation.status == "stopped",
        )
        .values(status="active")
    )
    await db.commit()
    return {"ok": True, "resumed": len(req.phones)}


# ---------------------------------------------------------------------------
# Instances
# ---------------------------------------------------------------------------

@router.get("/api/telegram/instances", dependencies=[Depends(require_api_key)])
async def tg_instances_endpoint(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(select(TelegramInstance).order_by(TelegramInstance.created_at))
    instances = list(result.scalars().all())
    stats = get_tg_instance_stats()

    items = []
    for inst in instances:
        phone = inst.phone_number
        warmup = get_tg_warmup_status(inst)
        inst_stats = stats.get(phone, {"hourly": 0, "daily": 0})
        rr = await get_tg_reply_rate(db, phone)
        br = await get_tg_block_rate(db, phone)

        items.append({
            "id": str(inst.id),
            "name": inst.name,
            "phone_number": phone,
            "is_authorized": inst.is_authorized,
            "is_active": inst.is_active,
            "is_banned": inst.is_banned,
            "health_status": inst.health_status,
            "flood_wait_count": inst.flood_wait_count,
            "connected": get_client(phone) is not None,
            "warmup": warmup,
            "stats": inst_stats,
            "reply_rate": {"value": rr, "classification": classify_tg_reply_rate(rr)},
            "block_rate": {"value": br, "classification": classify_tg_block_rate(br)},
            "last_send_at": inst.last_send_at.isoformat() if inst.last_send_at else None,
            "last_health_check": inst.last_health_check.isoformat() if inst.last_health_check else None,
            "created_at": inst.created_at.isoformat(),
        })

    return {"instances": items, "total": len(items)}


@router.post("/api/telegram/instances", dependencies=[Depends(require_api_key)])
async def tg_create_instance_endpoint(
    req: TGInstanceCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Register a new TelegramInstance row (without session — run telegram_auth.py
    separately to authenticate and populate the session_string).
    """
    result = await db.execute(
        select(TelegramInstance).where(TelegramInstance.phone_number == req.phone_number)
    )
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"Instance with phone {req.phone_number} already exists")

    inst = TelegramInstance(
        name=req.name,
        phone_number=req.phone_number,
        api_id=req.api_id,
        api_hash=req.api_hash,
        daily_limit=req.daily_limit,
        hourly_limit=req.hourly_limit,
        min_delay_sec=req.min_delay_sec,
        max_delay_sec=req.max_delay_sec,
        is_authorized=False,
        is_active=False,
    )
    db.add(inst)
    await db.commit()
    await db.refresh(inst)

    return {
        "id": str(inst.id),
        "phone_number": inst.phone_number,
        "is_authorized": inst.is_authorized,
        "message": "Instance created. Run 'python telegram_auth.py' to authenticate.",
    }


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@router.get("/api/telegram/stats/reply-rates", dependencies=[Depends(require_api_key)])
async def tg_reply_rates_endpoint(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(select(TelegramInstance))
    instances = list(result.scalars().all())

    items = []
    for inst in instances:
        phone = inst.phone_number
        rr = await get_tg_reply_rate(db, phone)
        br = await get_tg_block_rate(db, phone)
        rr_cls = classify_tg_reply_rate(rr)
        br_cls = classify_tg_block_rate(br)
        severity = max(
            ["no_data", "ok", "warning", "danger"].index(rr_cls),
            ["no_data", "ok", "warning", "danger"].index(br_cls),
        )
        items.append({
            "phone_number": phone,
            "name": inst.name,
            "reply_rate": {"value": rr, "classification": rr_cls},
            "block_rate": {"value": br, "classification": br_cls},
            "severity": ["no_data", "ok", "warning", "danger"][severity],
            "thresholds": {
                "reply_warning": TG_REPLY_RATE_WARNING,
                "reply_danger": TG_REPLY_RATE_DANGER,
                "block_warning": TG_BLOCK_RATE_WARNING,
                "block_danger": TG_BLOCK_RATE_DANGER,
            },
        })

    items.sort(
        key=lambda x: ["no_data", "ok", "warning", "danger"].index(x["severity"]),
        reverse=True,
    )
    return {"instances": items}
