import logging
import asyncio
import random
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import Integer, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Campaign, Conversation, WhatsAppInstance
from app.db.session import AsyncSessionLocal, get_db
from app.services.elevenlabs import generate_text_reply, get_agent_prompt
from app.services import pool as instance_pool
from app.services.blacklist import is_blacklisted
from app.services.country import detect_country
from app.services.link_pool import claim_link, load_links
from app.services.sender import send_initial_message
from app.webhook.handler import handle_incoming

logger = logging.getLogger(__name__)

router = APIRouter()

async def _resolve_initial_message(
    campaign: Campaign,
    lead_name: Optional[str],
    phone: str,
    provided: Optional[str],
    language: Optional[str] = None,
    link_url: Optional[str] = None,
    promo_code: Optional[str] = None,
) -> list[str]:
    import phonenumbers
    
    country = "PT"
    try:
        parsed = phonenumbers.parse(f"+{phone.lstrip('+')}")
        country = phonenumbers.region_code_for_number(parsed)
    except Exception:
        pass

    if country == "AR" or language == "es":
        # Argentina / Pampas script
        msg1 = f"¡Hola! Acá Olivia de Pampas 🙂 Fue un placer charlar con vos."
        msg2 = f"Como te prometí, acá tenés el link para tu bono del 175% en tu próximo dep. desde ARS 5000 · Solo por 5 días 👉 {link_url}"
        msg3 = "El link se va a habilitar para hacer clic si respondés con cualquier mensaje en este chat (¡hasta un emoji sirve!) :) Muchos éxitos 🤞😉"
    else:
        # Default European Portuguese / Oro Casino script
        msg1 = f"Olá! Aqui é a Camila do Oro Casino 🙂 Foi um prazer falar contigo."
        msg2 = f"Como prometido, aqui está o teu código promocional: 50Pragmatic. 50 Rodadas Grátis na Pragmatic Play · Apenas por 5 dias 👉 {link_url}"
        msg3 = "O link ficará clicável se me enviares qualquer mensagem de volta neste chat (nem que seja um emoji) :) Boa sorte 🤞😉"

    return [msg1, msg2, msg3]


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
    lead_id: Optional[str] = None           # auto-filled from phone if not provided
    lead_name: Optional[str] = None
    campaign_external_id: Optional[str] = None  # auto-detected from phone prefix if omitted
    initial_message: Optional[str] = None
    batch_index: int = 0


class LinkLoadItem(BaseModel):
    url: str
    country: str  # "PT", "AR", etc.


class LinkLoadRequest(BaseModel):
    links: list[LinkLoadItem]


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
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # 1. Detect country from phone
    country_info = detect_country(req.phone)
    country_code = country_info["code"]
    lang = country_info["lang"]
    promo = country_info["promo"]
    campaign_key = req.campaign_external_id or country_info["campaign"]
    lead_id = req.lead_id or req.phone

    logger.info(f"Incoming /api/send phone={req.phone} country={country_code} campaign={campaign_key} lang={lang}")

    # 2. Blacklist check
    if await is_blacklisted(db, req.phone):
        return {"status": "blacklisted"}

    # 3. Claim a link from the pool (block if exhausted)
    link_url = await claim_link(db, country_code, lead_id)
    if link_url is None:
        raise HTTPException(
            status_code=503,
            detail=f"Link pool exhausted for country={country_code}. Load more links via POST /api/links/load",
        )

    # 4. Find or auto-create campaign
    result = await db.execute(
        select(Campaign).where(Campaign.external_id == campaign_key)
    )
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
        logger.info(f"Auto-created campaign '{campaign_key}' for country={country_code}")

    # 5. Find or create conversation
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
            lead_id=lead_id,
            phone=req.phone,
            lead_name=req.lead_name,
            status="active",
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
    elif conversation.status == "stopped":
        return {"status": "skipped"}

    # 6. Build initial message
    initial_text = await _resolve_initial_message(
        campaign=campaign,
        lead_name=req.lead_name,
        phone=req.phone,
        provided=req.initial_message,
        language=lang,
        link_url=link_url,
        promo_code=promo,
    )

    # Helper function to run send in a fresh session so the current HTTP request can close
    async def bg_send_task(conv_id: str, texts: list[str], batch_idx: int):
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as bg_db:
            res = await bg_db.execute(select(Conversation).where(Conversation.id == conv_id))
            conv = res.scalar_one_or_none()
            if conv:
                await send_initial_message(bg_db, conv, texts, batch_idx)

    # Queue the background task
    background_tasks.add_task(bg_send_task, conversation.id, initial_text, req.batch_index)

    return {
        "status": "queued",
        "conversation_id": str(conversation.id),
        "country": country_code,
        "lang": lang,
        "link_url": link_url,
    }


@router.post("/api/links/load", dependencies=[Depends(require_api_key)])
async def load_links_endpoint(
    req: LinkLoadRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Bulk-load affiliate links into the pool."""
    items = [{"url": item.url, "country": item.country} for item in req.links]
    result = await load_links(db, items)
    return result


@router.get("/api/links/stats", dependencies=[Depends(require_api_key)])
async def links_stats_endpoint(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Show how many links are free/used per country."""
    from sqlalchemy import func as sqlfunc
    from app.db.models import LinkPool
    rows = await db.execute(
        select(
            LinkPool.country,
            sqlfunc.count().label("total"),
            sqlfunc.sum((~LinkPool.used).cast(Integer)).label("free"),
        ).group_by(LinkPool.country)
    )
    stats = [
        {"country": r.country, "total": r.total, "free": r.free or 0}
        for r in rows.all()
    ]
    return {"stats": stats}


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

        # Determine country and link
        country_info = detect_country(lead.phone)
        country_code = country_info["code"]
        lang = country_info["lang"]
        promo = country_info["promo"]
        lead_id = lead.lead_id or lead.phone

        link_url = await claim_link(db, country_code, lead_id)
        if link_url is None:
            results.append({"phone": lead.phone, "status": "error", "detail": f"link pool exhausted for {country_code}"})
            continue

        # Determine initial message via ElevenLabs (returns list[str])
        try:
            initial_text = await _resolve_initial_message(
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
