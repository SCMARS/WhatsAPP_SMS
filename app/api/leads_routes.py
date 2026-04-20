"""
Leads & Analytics API

Endpoints:
  GET  /api/leads                     — paginated leads list with filters
  GET  /api/leads/{phone}/conversation — full message timeline for a lead
  PATCH /api/leads/{phone}/status     — manually update lead_status
  POST /api/leads/{phone}/note        — add a note to a lead
  POST /api/leads/{phone}/event       — log a manual event (e.g. converted)
  GET  /api/analytics/overview        — key dashboard metrics
  GET  /api/analytics/funnel          — conversion funnel counts
  GET  /api/analytics/daily           — daily activity (sent/replies) chart data
  GET  /api/leads/export.csv          — CSV export of all leads
"""

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Campaign, Conversation, LeadEvent, WhatsAppMessage, TelegramMessage
from app.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Auth ──────────────────────────────────────────────────────────────────────

async def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if x_api_key != settings.API_SECRET_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


# ── Schemas ───────────────────────────────────────────────────────────────────

class LeadStatusUpdate(BaseModel):
    lead_status: str  # new | contacted | replied | interested | converted | unsubscribed


class NoteRequest(BaseModel):
    note: str


class ManualEventRequest(BaseModel):
    event_type: str
    note: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

VALID_STATUSES = {"new", "contacted", "replied", "interested", "converted", "unsubscribed"}


def _fmt(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _conv_to_dict(c: Conversation, campaign_name: str = "") -> dict[str, Any]:
    return {
        "id": str(c.id),
        "phone": c.phone,
        "lead_name": c.lead_name,
        "lead_id": c.lead_id,
        "platform": c.platform,
        "campaign": campaign_name or str(c.campaign_id),
        "lead_status": c.lead_status or "new",
        "status": c.status,
        "is_blacklisted": c.is_blacklisted,
        "outbound_count": c.outbound_count or 0,
        "reply_count": c.reply_count or 0,
        "assigned_link_url": c.assigned_link_url,
        "notes": c.notes,
        "first_contact_at": _fmt(c.first_contact_at),
        "replied_at": _fmt(c.replied_at),
        "last_activity_at": _fmt(c.last_activity_at),
        "created_at": _fmt(c.created_at),
    }


# ── GET /api/leads ────────────────────────────────────────────────────────────

@router.get("/api/leads", dependencies=[Depends(require_api_key)])
async def list_leads(
    db: AsyncSession = Depends(get_db),
    platform: Optional[str] = Query(None, description="whatsapp | telegram"),
    lead_status: Optional[str] = Query(None, description="new|contacted|replied|interested|converted|unsubscribed"),
    campaign: Optional[str] = Query(None, description="campaign external_id"),
    search: Optional[str] = Query(None, description="phone or name fragment"),
    date_from: Optional[str] = Query(None, description="ISO date, e.g. 2025-01-01"),
    date_to: Optional[str] = Query(None, description="ISO date, e.g. 2025-12-31"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    q = (
        select(Conversation, Campaign.external_id.label("campaign_ext"))
        .join(Campaign, Campaign.id == Conversation.campaign_id)
        .order_by(Conversation.last_activity_at.desc().nullslast(), Conversation.created_at.desc())
    )

    if platform:
        q = q.where(Conversation.platform == platform)
    if lead_status:
        q = q.where(Conversation.lead_status == lead_status)
    if campaign:
        q = q.where(Campaign.external_id == campaign)
    if search:
        like = f"%{search}%"
        q = q.where(
            (Conversation.phone.ilike(like)) | (Conversation.lead_name.ilike(like))
        )
    if date_from:
        try:
            dt = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
            q = q.where(Conversation.created_at >= dt)
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc)
            q = q.where(Conversation.created_at <= dt)
        except ValueError:
            pass

    # Total count
    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    rows = (await db.execute(q.limit(limit).offset(offset))).all()
    leads = [_conv_to_dict(row.Conversation, row.campaign_ext) for row in rows]

    return {"total": total, "limit": limit, "offset": offset, "leads": leads}


# ── GET /api/leads/{phone}/conversation ──────────────────────────────────────

@router.get("/api/leads/{phone}/conversation", dependencies=[Depends(require_api_key)])
async def get_lead_conversation(
    phone: str,
    db: AsyncSession = Depends(get_db),
    platform: Optional[str] = Query(None),
) -> dict[str, Any]:
    """Return full message history + events for a lead phone number."""
    digits = "".join(c for c in phone if c.isdigit())
    phone_variants = [phone]
    if digits:
        phone_variants.append(digits)
        phone_variants.append(f"+{digits}")

    q = (
        select(Conversation, Campaign.external_id.label("campaign_ext"))
        .join(Campaign, Campaign.id == Conversation.campaign_id)
        .where(Conversation.phone.in_(phone_variants))
        .order_by(Conversation.created_at.desc())
    )
    if platform:
        q = q.where(Conversation.platform == platform)

    rows = (await db.execute(q)).all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"No conversation found for phone={phone}")

    result = []
    for row in rows:
        conv: Conversation = row.Conversation
        campaign_ext: str = row.campaign_ext

        # WhatsApp messages
        wa_msgs = (await db.execute(
            select(WhatsAppMessage)
            .where(WhatsAppMessage.conversation_id == conv.id)
            .order_by(WhatsAppMessage.created_at)
        )).scalars().all()

        # Telegram messages
        tg_msgs = (await db.execute(
            select(TelegramMessage)
            .where(TelegramMessage.conversation_id == conv.id)
            .order_by(TelegramMessage.created_at)
        )).scalars().all()

        # Lead events
        events = (await db.execute(
            select(LeadEvent)
            .where(LeadEvent.conversation_id == conv.id)
            .order_by(LeadEvent.created_at)
        )).scalars().all()

        # Merge messages into unified timeline
        messages = []
        for m in wa_msgs:
            messages.append({
                "id": str(m.id),
                "platform": "whatsapp",
                "direction": m.direction,
                "body": m.body,
                "status": m.status,
                "error": m.error,
                "provider_message_id": m.provider_message_id,
                "created_at": _fmt(m.created_at),
                "meta": m.meta,
            })
        for m in tg_msgs:
            messages.append({
                "id": str(m.id),
                "platform": "telegram",
                "direction": m.direction,
                "body": m.body,
                "status": m.status,
                "error": m.error,
                "provider_message_id": m.provider_message_id,
                "created_at": _fmt(m.created_at),
                "meta": m.meta,
            })
        messages.sort(key=lambda x: x["created_at"] or "")

        result.append({
            **_conv_to_dict(conv, campaign_ext),
            "messages": messages,
            "events": [
                {
                    "id": str(e.id),
                    "event_type": e.event_type,
                    "note": e.note,
                    "meta": e.meta,
                    "created_at": _fmt(e.created_at),
                }
                for e in events
            ],
        })

    return {"conversations": result}


# ── PATCH /api/leads/{phone}/status ──────────────────────────────────────────

@router.patch("/api/leads/{phone}/status", dependencies=[Depends(require_api_key)])
async def update_lead_status(
    phone: str,
    req: LeadStatusUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if req.lead_status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid lead_status. Valid: {VALID_STATUSES}")

    digits = "".join(c for c in phone if c.isdigit())
    phone_variants = [phone, digits, f"+{digits}"]

    result = await db.execute(
        select(Conversation)
        .where(Conversation.phone.in_(phone_variants))
        .order_by(Conversation.created_at.desc())
        .limit(1)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail=f"No conversation for phone={phone}")

    old_status = conv.lead_status
    now_utc = datetime.now(timezone.utc)
    conv.lead_status = req.lead_status
    conv.last_activity_at = now_utc
    if req.lead_status == "converted" and conv.replied_at is None:
        conv.replied_at = now_utc

    db.add(LeadEvent(
        conversation_id=conv.id,
        event_type="status_changed",
        note=f"{old_status} → {req.lead_status}",
    ))
    db.add(conv)
    await db.commit()

    return {"ok": True, "phone": phone, "lead_status": conv.lead_status}


# ── POST /api/leads/{phone}/note ──────────────────────────────────────────────

@router.post("/api/leads/{phone}/note", dependencies=[Depends(require_api_key)])
async def add_lead_note(
    phone: str,
    req: NoteRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not req.note.strip():
        raise HTTPException(status_code=422, detail="note cannot be empty")

    digits = "".join(c for c in phone if c.isdigit())
    phone_variants = [phone, digits, f"+{digits}"]

    result = await db.execute(
        select(Conversation)
        .where(Conversation.phone.in_(phone_variants))
        .order_by(Conversation.created_at.desc())
        .limit(1)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail=f"No conversation for phone={phone}")

    if conv.notes:
        conv.notes = f"{conv.notes}\n---\n{req.note.strip()}"
    else:
        conv.notes = req.note.strip()
    conv.last_activity_at = datetime.now(timezone.utc)

    db.add(LeadEvent(
        conversation_id=conv.id,
        event_type="note_added",
        note=req.note.strip()[:500],
    ))
    db.add(conv)
    await db.commit()

    return {"ok": True, "phone": phone, "notes": conv.notes}


# ── POST /api/leads/{phone}/event ─────────────────────────────────────────────

@router.post("/api/leads/{phone}/event", dependencies=[Depends(require_api_key)])
async def add_lead_event(
    phone: str,
    req: ManualEventRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    digits = "".join(c for c in phone if c.isdigit())
    phone_variants = [phone, digits, f"+{digits}"]

    result = await db.execute(
        select(Conversation)
        .where(Conversation.phone.in_(phone_variants))
        .order_by(Conversation.created_at.desc())
        .limit(1)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail=f"No conversation for phone={phone}")

    db.add(LeadEvent(
        conversation_id=conv.id,
        event_type=req.event_type,
        note=req.note,
    ))

    # Auto-advance status for known event types
    if req.event_type == "converted" and conv.lead_status not in ("converted",):
        conv.lead_status = "converted"
    elif req.event_type == "link_clicked" and conv.lead_status in ("new", "contacted", "replied"):
        conv.lead_status = "interested"
    conv.last_activity_at = datetime.now(timezone.utc)
    db.add(conv)

    await db.commit()
    return {"ok": True, "phone": phone, "event_type": req.event_type}


# ── GET /api/analytics/overview ───────────────────────────────────────────────

@router.get("/api/analytics/overview", dependencies=[Depends(require_api_key)])
async def analytics_overview(
    db: AsyncSession = Depends(get_db),
    platform: Optional[str] = Query(None),
    campaign: Optional[str] = Query(None),
) -> dict[str, Any]:
    base = select(Conversation).join(Campaign, Campaign.id == Conversation.campaign_id)
    if platform:
        base = base.where(Conversation.platform == platform)
    if campaign:
        base = base.where(Campaign.external_id == campaign)

    def _count(extra_where=None):
        q = select(func.count()).select_from(
            base.where(extra_where).subquery() if extra_where is not None else base.subquery()
        )
        return q

    total = (await db.execute(_count())).scalar_one()
    contacted = (await db.execute(_count(Conversation.outbound_count > 0))).scalar_one()
    replied = (await db.execute(_count(Conversation.reply_count > 0))).scalar_one()
    interested = (await db.execute(_count(Conversation.lead_status == "interested"))).scalar_one()
    converted = (await db.execute(_count(Conversation.lead_status == "converted"))).scalar_one()
    unsubscribed = (await db.execute(_count(Conversation.lead_status == "unsubscribed"))).scalar_one()

    # Total messages sent (WhatsApp + Telegram)
    wa_sent = (await db.execute(
        select(func.count()).select_from(
            select(WhatsAppMessage)
            .join(Conversation, Conversation.id == WhatsAppMessage.conversation_id)
            .where(WhatsAppMessage.direction == "outbound")
            .subquery()
        )
    )).scalar_one()
    tg_sent = (await db.execute(
        select(func.count()).select_from(
            select(TelegramMessage)
            .join(Conversation, Conversation.id == TelegramMessage.conversation_id)
            .where(TelegramMessage.direction == "outbound")
            .subquery()
        )
    )).scalar_one()
    total_sent = wa_sent + tg_sent

    wa_recv = (await db.execute(
        select(func.count()).select_from(
            select(WhatsAppMessage)
            .join(Conversation, Conversation.id == WhatsAppMessage.conversation_id)
            .where(WhatsAppMessage.direction == "inbound")
            .subquery()
        )
    )).scalar_one()
    tg_recv = (await db.execute(
        select(func.count()).select_from(
            select(TelegramMessage)
            .join(Conversation, Conversation.id == TelegramMessage.conversation_id)
            .where(TelegramMessage.direction == "inbound")
            .subquery()
        )
    )).scalar_one()
    total_received = wa_recv + tg_recv

    reply_rate = round(replied / contacted * 100, 1) if contacted else 0.0
    conversion_rate = round(converted / replied * 100, 1) if replied else 0.0

    return {
        "total_leads": total,
        "contacted": contacted,
        "replied": replied,
        "interested": interested,
        "converted": converted,
        "unsubscribed": unsubscribed,
        "reply_rate_pct": reply_rate,
        "conversion_rate_pct": conversion_rate,
        "total_messages_sent": total_sent,
        "total_messages_received": total_received,
    }


# ── GET /api/analytics/funnel ─────────────────────────────────────────────────

@router.get("/api/analytics/funnel", dependencies=[Depends(require_api_key)])
async def analytics_funnel(
    db: AsyncSession = Depends(get_db),
    platform: Optional[str] = Query(None),
    campaign: Optional[str] = Query(None),
) -> dict[str, Any]:
    q = (
        select(Conversation.lead_status, func.count().label("cnt"))
        .join(Campaign, Campaign.id == Conversation.campaign_id)
        .group_by(Conversation.lead_status)
    )
    if platform:
        q = q.where(Conversation.platform == platform)
    if campaign:
        q = q.where(Campaign.external_id == campaign)

    rows = (await db.execute(q)).all()

    counts = {r.lead_status: r.cnt for r in rows}

    stages = ["new", "contacted", "replied", "interested", "converted", "unsubscribed"]
    funnel = [{"stage": s, "count": counts.get(s, 0)} for s in stages]
    # Add any custom stages not in the default list
    for s, c in counts.items():
        if s not in stages:
            funnel.append({"stage": s, "count": c})

    return {"funnel": funnel}


# ── GET /api/analytics/daily ──────────────────────────────────────────────────

@router.get("/api/analytics/daily", dependencies=[Depends(require_api_key)])
async def analytics_daily(
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
    platform: Optional[str] = Query(None),
) -> dict[str, Any]:
    """Daily sent/received message counts for the last N days (WhatsApp + Telegram)."""
    # Build platform filter safely (no f-string injection)
    platform_clause = "AND c.platform = :platform" if platform else ""

    # Uses UNION ALL of WA + TG messages; INTERVAL via bind-param multiplication
    sql = text(f"""
        SELECT
            DATE(m.created_at AT TIME ZONE 'UTC') AS day,
            m.direction,
            COUNT(*) AS cnt
        FROM (
            SELECT wa.created_at, wa.direction, wa.conversation_id
            FROM whatsapp_messages wa
            UNION ALL
            SELECT tg.created_at, tg.direction, tg.conversation_id
            FROM telegram_messages tg
        ) AS m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.created_at >= NOW() - (:days * INTERVAL '1 day')
        {platform_clause}
        GROUP BY 1, 2
        ORDER BY 1
    """)

    params: dict[str, Any] = {"days": days}
    if platform:
        params["platform"] = platform

    rows = (await db.execute(sql, params)).all()

    data: dict[str, dict[str, int]] = {}
    for row in rows:
        day_str = str(row.day)
        if day_str not in data:
            data[day_str] = {"sent": 0, "received": 0}
        if row.direction == "outbound":
            data[day_str]["sent"] = row.cnt
        else:
            data[day_str]["received"] = row.cnt

    return {
        "days": days,
        "data": [
            {"date": d, "sent": v["sent"], "received": v["received"]}
            for d, v in sorted(data.items())
        ],
    }


# ── GET /api/leads/export.csv ─────────────────────────────────────────────────

@router.get("/api/leads/export.csv", dependencies=[Depends(require_api_key)])
async def export_leads_csv(
    db: AsyncSession = Depends(get_db),
    platform: Optional[str] = Query(None),
    lead_status: Optional[str] = Query(None),
    campaign: Optional[str] = Query(None),
) -> StreamingResponse:
    q = (
        select(Conversation, Campaign.external_id.label("campaign_ext"))
        .join(Campaign, Campaign.id == Conversation.campaign_id)
        .order_by(Conversation.created_at.desc())
    )
    if platform:
        q = q.where(Conversation.platform == platform)
    if lead_status:
        q = q.where(Conversation.lead_status == lead_status)
    if campaign:
        q = q.where(Campaign.external_id == campaign)

    rows = (await db.execute(q)).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "phone", "lead_name", "lead_id", "platform", "campaign",
        "lead_status", "outbound_count", "reply_count",
        "assigned_link_url", "is_blacklisted",
        "first_contact_at", "replied_at", "last_activity_at",
        "notes", "created_at",
    ])
    for row in rows:
        c = row.Conversation
        writer.writerow([
            c.phone, c.lead_name or "", c.lead_id, c.platform, row.campaign_ext,
            c.lead_status or "new", c.outbound_count or 0, c.reply_count or 0,
            c.assigned_link_url or "", c.is_blacklisted,
            _fmt(c.first_contact_at) or "", _fmt(c.replied_at) or "",
            _fmt(c.last_activity_at) or "", (c.notes or "").replace("\n", " | "),
            _fmt(c.created_at) or "",
        ])

    output.seek(0)
    filename = f"leads_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
