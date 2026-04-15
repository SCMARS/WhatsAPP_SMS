import logging
import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import Integer, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Campaign, Conversation, WhatsAppInstance, WhatsAppMessage
from app.db.session import AsyncSessionLocal, get_db
from app.services.elevenlabs import build_outreach_parts, generate_outreach_message
from app.services.green_api import set_anti_ban_settings, get_state_instance
from app.services import pool as instance_pool
from app.services.blacklist import is_blacklisted
from app.services.country import detect_country
from app.services.link_pool import claim_link, load_links
from app.services.rate_limiter import batch_pause
from app.services.sender import send_initial_message
from app.webhook.handler import handle_incoming

logger = logging.getLogger(__name__)

router = APIRouter()


def _to_elevenlabs_language(lang: Optional[str]) -> str:
    if lang == "es":
        return "es-AR"
    if lang == "pt":
        return "pt-PT"
    return "pt-PT"


def _split_outreach_into_three_random_parts(
    text: str,
    *,
    link_url: Optional[str] = None,
    promo_code: Optional[str] = None,
) -> list[str]:
    """
    Split outreach text into 3 meaningful parts at sentence boundaries.
    Short fragments (< 30 chars) are merged with the next sentence so no part
    is just "¡Hola!" on its own. Splits always happen at sentence boundaries,
    never mid-sentence. If the link/promo land in a later chunk, the critical
    chunk is merged into the first message so the lead sees the CTA immediately.
    """
    MIN_PART_LEN = 30

    def _normalize_parts(parts: list[str]) -> list[str]:
        return [" ".join((part or "").split()).strip() for part in parts if (part or "").strip()]

    def _has_required_fields(part: str) -> bool:
        if link_url and link_url not in part:
            return False
        if promo_code and promo_code not in part:
            return False
        return True

    def _promote_critical_chunk(parts: list[str]) -> list[str]:
        normalized = _normalize_parts(parts)
        if len(normalized) <= 1 or _has_required_fields(normalized[0]):
            return normalized

        critical_indexes = [
            idx for idx, part in enumerate(normalized)
            if (link_url and link_url in part) or (promo_code and promo_code in part)
        ]
        if not critical_indexes:
            return normalized

        merged_indexes = {0, *critical_indexes}
        first_part = " ".join(normalized[idx] for idx in sorted(merged_indexes)).strip()
        remaining = [part for idx, part in enumerate(normalized) if idx not in merged_indexes]
        return [first_part, *remaining]

    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return []

    # Split on sentence boundaries (.!?) followed by whitespace.
    raw_sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", cleaned)
        if part.strip()
    ]

    # Merge short fragments with the next sentence so each chunk is meaningful.
    merged: list[str] = []
    buf = ""
    for sent in raw_sentences:
        buf = f"{buf} {sent}".strip() if buf else sent
        if len(buf) >= MIN_PART_LEN:
            merged.append(buf)
            buf = ""
    if buf:
        if merged:
            merged[-1] = f"{merged[-1]} {buf}"
        else:
            merged.append(buf)

    if len(merged) >= 3:
        parts = merged[:3]
        if len(merged) > 3:
            parts[-1] = f"{parts[-1]} {' '.join(merged[3:])}".strip()
        return _promote_critical_chunk(parts)

    def _split_chunk_at_sentence_boundary(chunk: str) -> tuple[str, str]:
        """Split chunk into two at the sentence boundary nearest to the midpoint."""
        subs = [s.strip() for s in re.split(r"(?<=[.!?])\s+", chunk) if s.strip()]
        if len(subs) < 2:
            # No sentence boundary — split by words at midpoint
            words = chunk.split()
            mid = max(1, len(words) // 2)
            return " ".join(words[:mid]), " ".join(words[mid:]) or chunk
        mid_char = len(chunk) / 2
        pos = 0
        best_split = 1
        best_dist = float("inf")
        for i, s in enumerate(subs[:-1]):
            pos += len(s) + 1
            dist = abs(pos - mid_char)
            if dist < best_dist:
                best_dist = dist
                best_split = i + 1
        return " ".join(subs[:best_split]), " ".join(subs[best_split:])

    if len(merged) == 2:
        longer_idx = 0 if len(merged[0]) >= len(merged[1]) else 1
        shorter_idx = 1 - longer_idx
        half_a, half_b = _split_chunk_at_sentence_boundary(merged[longer_idx])
        if longer_idx == 0:
            parts = [half_a, half_b, merged[shorter_idx]]
        else:
            parts = [merged[shorter_idx], half_a, half_b]
        return _promote_critical_chunk(parts)

    # Single chunk — split into thirds by sentence boundary.
    half_a, rest = _split_chunk_at_sentence_boundary(cleaned)
    half_b, half_c = _split_chunk_at_sentence_boundary(rest)
    parts = [p for p in [half_a, half_b, half_c] if p]
    while len(parts) < 3:
        parts.append(cleaned)
    return _promote_critical_chunk(parts[:3])


def _opening_key(text: str, words: int = 6) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip().lower())
    cleaned = re.sub(r"[^\w\s]", "", cleaned)
    tokenized = [t for t in cleaned.split(" ") if t]
    return " ".join(tokenized[:words])


async def _recent_opening_keys(db: AsyncSession, phone: str, limit: int = 12) -> set[str]:
    rows = await db.execute(
        select(WhatsAppMessage.body)
        .join(Conversation, Conversation.id == WhatsAppMessage.conversation_id)
        .where(
            Conversation.phone == phone,
            WhatsAppMessage.direction == "outbound",
            WhatsAppMessage.body.isnot(None),
        )
        .order_by(WhatsAppMessage.created_at.desc())
        .limit(limit)
    )
    bodies = [b for b in rows.scalars().all() if b]
    return {_opening_key(b) for b in bodies if _opening_key(b)}


async def _resolve_initial_message(
    db: AsyncSession,
    campaign: Campaign,
    lead_name: Optional[str],  # reserved, not injected into message
    phone: str,
    provided: Optional[str],
    language: Optional[str] = None,
    link_url: Optional[str] = None,
    promo_code: Optional[str] = None,
) -> list[str]:
    # Caller-supplied message takes priority (manual override).
    if provided:
        return [provided] if isinstance(provided, str) else list(provided)

    resolved_lang = _to_elevenlabs_language(language)
    recent_openings = await _recent_opening_keys(db, phone, limit=12)

    # --- Structured 3-part outreach (template-based) ---
    # Try up to 4 times to pick a greeting that wasn't used recently.
    max_attempts = 4
    for _attempt in range(max_attempts):
        parts = build_outreach_parts(resolved_lang, link_url or "", promo_code or "")
        if _opening_key(parts[0]) not in recent_openings:
            return parts
    # All greetings collided — return last generated set anyway.
    return parts


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


@router.get("/api/config", dependencies=[Depends(require_api_key)])
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

    if country_code not in ("PT", "AR"):
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported country for outbound link pool: {country_code}",
        )

    # 2. Blacklist check — normalize to digits so "+380..." matches "380..." in DB
    _phone_digits = "".join(c for c in req.phone if c.isdigit())
    if await is_blacklisted(db, _phone_digits) or await is_blacklisted(db, req.phone):
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
        db=db,
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
    for i, lead in enumerate(req.leads):
        # Fire batch pause BEFORE starting each new batch of 10 (not after)
        if i > 0 and i % 10 == 0:
            await batch_pause(i)

        # Override campaign_external_id from parent request if not set per-lead
        if not lead.campaign_external_id:
            lead.campaign_external_id = req.campaign_external_id

        # Blacklist check — normalize to digits for consistent matching
        _lead_digits = "".join(c for c in lead.phone if c.isdigit())
        if await is_blacklisted(db, _lead_digits) or await is_blacklisted(db, lead.phone):
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

        if country_code not in ("PT", "AR"):
            results.append({"phone": lead.phone, "status": "error", "detail": f"unsupported country: {country_code}"})
            continue

        link_url = await claim_link(db, country_code, lead_id)
        if link_url is None:
            results.append({"phone": lead.phone, "status": "error", "detail": f"link pool exhausted for {country_code}"})
            continue

        # Determine initial message via ElevenLabs (returns list[str])
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
            logger.error(f"Unexpected error while resolving initial message for {lead.phone}: {e}")
            results.append({"phone": lead.phone, "status": "error", "detail": "internal error while building initial message"})
            continue

        # Send
        msg = await send_initial_message(
            db=db,
            conversation=conversation,
            initial_text=initial_text,
            batch_index=i,
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

    # Apply anti-ban settings immediately after registration
    state = await get_state_instance(req.instance_id, req.api_token)
    anti_ban_ok = False
    if state == "authorized":
        anti_ban_ok = await set_anti_ban_settings(req.instance_id, req.api_token)
    logger.info(
        f"New instance {req.instance_id} registered: state={state} anti_ban_applied={anti_ban_ok}"
    )

    return {
        "id": str(instance.id),
        "instance_id": instance.instance_id,
        "name": instance.name,
        "is_active": instance.is_active,
        "state": state,
        "anti_ban_applied": anti_ban_ok,
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
    from app.services.reply_monitor import (
        classify_block_rate,
        classify_reply_rate,
        get_all_block_rates,
        get_all_reply_rates,
    )
    from app.services.pool import get_warmup_status

    result = await db.execute(select(WhatsAppInstance))
    instances = result.scalars().all()
    stats        = instance_pool.get_instance_stats()
    reply_rates  = await get_all_reply_rates(db)
    block_rates  = await get_all_block_rates(db)

    data = []
    for inst in instances:
        inst_stats  = stats.get(inst.instance_id, {"hourly": 0, "daily": 0})
        rr          = reply_rates.get(inst.instance_id)
        br          = block_rates.get(inst.instance_id)
        warmup      = get_warmup_status(inst)
        data.append({
            "id": str(inst.id),
            "name": inst.name,
            "instance_id": inst.instance_id,
            "phone_number": inst.phone_number,
            "is_active": inst.is_active,
            "is_banned": inst.is_banned,
            "daily_limit": inst.daily_limit,
            "hourly_limit": inst.hourly_limit,
            "eff_daily_limit":  warmup["eff_daily"],
            "eff_hourly_limit": warmup["eff_hourly"],
            "hourly_sent": inst_stats["hourly"],
            "daily_sent":  inst_stats["daily"],
            "health_status": inst.health_status,
            # Reply rate
            "reply_rate_7d":     round(rr, 4) if rr is not None else None,
            "reply_rate_status": classify_reply_rate(rr),
            # Block rate
            "block_rate_7d":     round(br, 4) if br is not None else None,
            "block_rate_status": classify_block_rate(br),
            # Warmup
            "in_warmup":           warmup["in_warmup"],
            "warmup_age_days":     warmup["age_days"],
            "warmup_days_left":    warmup["days_remaining"],
        })

    return {"instances": data, "total": len(data)}


@router.get("/api/stats/reply-rates", dependencies=[Depends(require_api_key)])
async def reply_rates_endpoint(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Full anti-ban health report: reply rate + block rate for all instances.
    Sort order: worst first (danger → warning → ok → no_data).
    """
    from app.services.reply_monitor import (
        BLOCK_RATE_DANGER,
        BLOCK_RATE_WARNING,
        REPLY_RATE_DANGER,
        REPLY_RATE_LOOKBACK_DAYS,
        REPLY_RATE_WARNING,
        classify_block_rate,
        classify_reply_rate,
        get_all_block_rates,
        get_all_reply_rates,
    )
    from app.services.pool import get_warmup_status
    from sqlalchemy import select as sa_select

    reply_rates = await get_all_reply_rates(db)
    block_rates = await get_all_block_rates(db)

    # Gather all instance_ids that appear in either map
    all_ids = set(reply_rates) | set(block_rates)

    # Also load warmup info for each instance
    inst_res = await db.execute(sa_select(WhatsAppInstance))
    inst_map = {i.instance_id: i for i in inst_res.scalars().all()}

    data = []
    for inst_id in all_ids:
        rr = reply_rates.get(inst_id)
        br = block_rates.get(inst_id)
        inst = inst_map.get(inst_id)
        warmup = get_warmup_status(inst) if inst else None

        rr_status = classify_reply_rate(rr)
        br_status = classify_block_rate(br)
        # Overall worst status
        _order = {"danger": 0, "warning": 1, "ok": 2, "no_data": 3}
        overall = min(rr_status, br_status, key=lambda s: _order.get(s, 9))

        entry = {
            "instance_id":       inst_id,
            "overall_status":    overall,
            "reply_rate":        round(rr, 4) if rr is not None else None,
            "reply_rate_pct":    f"{rr * 100:.1f}%" if rr is not None else "n/a",
            "reply_rate_status": rr_status,
            "block_rate":        round(br, 4) if br is not None else None,
            "block_rate_pct":    f"{br * 100:.2f}%" if br is not None else "n/a",
            "block_rate_status": br_status,
        }
        if warmup:
            entry["in_warmup"]        = warmup["in_warmup"]
            entry["warmup_age_days"]  = warmup["age_days"]
            entry["warmup_days_left"] = warmup["days_remaining"]
        data.append(entry)

    data.sort(key=lambda r: _order.get(r["overall_status"], 9))

    return {
        "lookback_days": REPLY_RATE_LOOKBACK_DAYS,
        "thresholds": {
            "reply_rate_warning": REPLY_RATE_WARNING,
            "reply_rate_danger":  REPLY_RATE_DANGER,
            "block_rate_warning": BLOCK_RATE_WARNING,
            "block_rate_danger":  BLOCK_RATE_DANGER,
        },
        "instances": data,
        "total": len(data),
    }
