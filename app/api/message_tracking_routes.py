"""API endpoints for message view tracking."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from datetime import datetime, timedelta, timezone
from app.db.database import get_db
from app.db.models import TelegramMessage, Conversation

router = APIRouter(prefix="/api/messages", tags=["messages"])
TZ = timezone.utc


@router.get("/status")
async def get_message_status(
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    """Get status of outbound Telegram messages with view tracking."""
    
    stmt = select(TelegramMessage).where(
        TelegramMessage.direction == "outbound"
    ).order_by(TelegramMessage.created_at.desc()).limit(limit)
    
    result = await db.execute(stmt)
    messages = result.scalars().all()
    
    output = []
    four_days = timedelta(days=4)
    now = datetime.now(TZ)
    
    for msg in messages:
        # Determine status
        if msg.replied_at:
            status = "REPLIED"
        elif msg.viewed_at:
            days_since_viewed = (now - msg.viewed_at).days
            if days_since_viewed >= 4:
                status = "EXPIRED"
            else:
                status = f"VIEWED ({4 - days_since_viewed}d left)"
        else:
            status = "SENT"
        
        # Get conversation details
        conv = await db.get(Conversation, msg.conversation_id)
        
        output.append({
            "id": str(msg.id),
            "phone": conv.phone if conv else "Unknown",
            "text": msg.body[:100],
            "sent_at": msg.created_at.isoformat() if msg.created_at else None,
            "viewed_at": msg.viewed_at.isoformat() if msg.viewed_at else None,
            "replied_at": msg.replied_at.isoformat() if msg.replied_at else None,
            "status": status,
        })
    
    return {
        "total": len(output),
        "messages": output,
    }


@router.get("/status/summary")
async def get_message_summary(db: AsyncSession = Depends(get_db)):
    """Get summary of message statuses."""
    
    four_days_ago = datetime.now(TZ) - timedelta(days=4)
    
    # Count by status
    replied = await db.scalar(
        select(func.count()).select_from(TelegramMessage).where(
            and_(
                TelegramMessage.direction == "outbound",
                TelegramMessage.replied_at.isnot(None),
            )
        )
    ) or 0
    
    viewed = await db.scalar(
        select(func.count()).select_from(TelegramMessage).where(
            and_(
                TelegramMessage.direction == "outbound",
                TelegramMessage.viewed_at.isnot(None),
                TelegramMessage.replied_at.is_(None),
                TelegramMessage.created_at > four_days_ago,
            )
        )
    ) or 0
    
    expired = await db.scalar(
        select(func.count()).select_from(TelegramMessage).where(
            and_(
                TelegramMessage.direction == "outbound",
                TelegramMessage.viewed_at.isnot(None),
                TelegramMessage.replied_at.is_(None),
                TelegramMessage.created_at <= four_days_ago,
            )
        )
    ) or 0
    
    sent = await db.scalar(
        select(func.count()).select_from(TelegramMessage).where(
            and_(
                TelegramMessage.direction == "outbound",
                TelegramMessage.viewed_at.is_(None),
            )
        )
    ) or 0
    
    return {
        "REPLIED": replied,
        "VIEWED": viewed,
        "EXPIRED": expired,
        "SENT": sent,
    }
