"""
Link pool service.
Atomically claims a free link for a given country from the links_pool table.
"""
import logging
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LinkPool

logger = logging.getLogger(__name__)


async def claim_link(db: AsyncSession, country: str, lead_id: str) -> Optional[str]:
    """
    Claim the next available (unused) link for a given country.
    Uses SKIP LOCKED to be safe under concurrent requests.
    Returns the URL string, or None if pool is exhausted.
    """
    # Find the oldest unused link for this country
    result = await db.execute(
        select(LinkPool)
        .where(LinkPool.country == country, LinkPool.used == False)
        .order_by(LinkPool.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    link = result.scalar_one_or_none()

    if link is None:
        logger.warning(f"Link pool exhausted for country={country}")
        return None

    # Mark as used
    from datetime import datetime, timezone
    link.used = True
    link.used_at = datetime.now(timezone.utc)
    link.lead_id = lead_id
    await db.commit()
    await db.refresh(link)

    logger.info(f"Claimed link id={link.id} url={link.url} for country={country} lead={lead_id}")
    return link.url


async def load_links(db: AsyncSession, links: list[dict]) -> dict:
    """
    Bulk-insert links into the pool.
    Skips duplicates by URL.
    links: list of {"url": str, "country": str}
    Returns {"loaded": N, "skipped": M}
    """
    from app.db.models import LinkPool

    loaded = 0
    skipped = 0

    for item in links:
        url = (item.get("url") or "").strip()
        country = (item.get("country") or "").strip().upper()
        if not url or not country:
            skipped += 1
            continue

        # Check for duplicate
        existing = await db.execute(
            select(LinkPool).where(LinkPool.url == url)
        )
        if existing.scalar_one_or_none():
            skipped += 1
            continue

        db.add(LinkPool(url=url, country=country))
        loaded += 1

    await db.commit()
    logger.info(f"Links loaded: {loaded}, skipped: {skipped}")
    return {"loaded": loaded, "skipped": skipped}
