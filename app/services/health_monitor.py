"""
Background health monitor for Green API WhatsApp instances.

Polls every POLL_INTERVAL_SEC (60 s) and reacts:
  - authorized    → mark healthy, ensure anti-ban settings are applied
  - yellowCard    → reboot, wait YELLOW_COOLDOWN_SEC (300 s), clear queue
  - blocked       → mark banned in DB, attempt unban (best-effort)
  - notAuthorized → mark inactive (QR re-scan needed), stop sending
  - sleepMode     → warn, continue (instance may self-recover)
  - unknown       → skip, retry next cycle

Also exposes `apply_anti_ban_to_all_instances()` called once on startup.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.db.models import WhatsAppInstance
from app.db.session import AsyncSessionLocal
from app.services.green_api import (
    clear_messages_queue,
    get_state_instance,
    reboot_instance,
    set_anti_ban_settings,
    unban_instance,
)
from app.services import pool as instance_pool

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 60
YELLOW_COOLDOWN_SEC = 300   # 5 minutes after reboot before resuming sends
REBOOT_SETTLE_SEC = 15      # time to wait between reboot and queue clear


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _update_health(instance_id: str, health_status: str, is_active: bool, is_banned: bool) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(WhatsAppInstance)
            .where(WhatsAppInstance.instance_id == instance_id)
            .values(
                health_status=health_status,
                last_health_check=datetime.now(timezone.utc),
                is_active=is_active,
                is_banned=is_banned,
            )
        )
        await db.commit()


async def _load_active_instances() -> list[WhatsAppInstance]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(WhatsAppInstance).where(
                WhatsAppInstance.is_banned == False,
            )
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Per-instance check
# ---------------------------------------------------------------------------

async def check_instance(inst: WhatsAppInstance) -> None:
    instance_id = inst.instance_id
    api_token = inst.api_token

    state = await get_state_instance(instance_id, api_token)
    logger.info(f"[HealthMonitor] {instance_id} state={state}")

    if state == "authorized":
        # Make sure anti-ban settings are in place every cycle
        await set_anti_ban_settings(instance_id, api_token)
        await _update_health(instance_id, "authorized", is_active=True, is_banned=False)

    elif state == "yellowCard":
        logger.warning(
            f"[HealthMonitor] {instance_id} yellowCard — rebooting and entering "
            f"{YELLOW_COOLDOWN_SEC}s cooldown"
        )
        await _update_health(instance_id, "yellowCard", is_active=False, is_banned=False)
        await reboot_instance(instance_id, api_token)
        await asyncio.sleep(REBOOT_SETTLE_SEC)
        await clear_messages_queue(instance_id, api_token)

        # Cooldown: instance stays inactive; re-activated when next check shows authorized
        await asyncio.sleep(YELLOW_COOLDOWN_SEC)

        # Re-check after cooldown
        new_state = await get_state_instance(instance_id, api_token)
        if new_state == "authorized":
            await set_anti_ban_settings(instance_id, api_token)
            await _update_health(instance_id, "authorized", is_active=True, is_banned=False)
            logger.info(f"[HealthMonitor] {instance_id} recovered from yellowCard")
        else:
            logger.warning(
                f"[HealthMonitor] {instance_id} still {new_state} after cooldown — staying inactive"
            )
            await _update_health(instance_id, new_state, is_active=False, is_banned=False)

    elif state == "blocked":
        logger.error(
            f"[HealthMonitor] {instance_id} BLOCKED by WhatsApp — marking banned, attempting unban"
        )
        await _update_health(instance_id, "blocked", is_active=False, is_banned=True)
        # Also purge from in-memory send log so the pool stops selecting it
        instance_pool._send_log.pop(instance_id, None)
        # Best-effort unban request (Green API passes the request to WhatsApp but success is not guaranteed)
        unbanned = await unban_instance(instance_id, api_token)
        if unbanned:
            logger.info(f"[HealthMonitor] Unban request sent for {instance_id} (success not guaranteed)")

    elif state == "notAuthorized":
        logger.warning(f"[HealthMonitor] {instance_id} notAuthorized — disabling until re-scanned")
        await _update_health(instance_id, "notAuthorized", is_active=False, is_banned=False)

    elif state == "sleepMode":
        logger.info(f"[HealthMonitor] {instance_id} in sleepMode — no action (will self-recover)")
        await _update_health(instance_id, "sleepMode", is_active=True, is_banned=False)

    else:
        # unknown or unexpected — skip, will retry next cycle
        logger.debug(f"[HealthMonitor] {instance_id} state={state}, skipping")


# ---------------------------------------------------------------------------
# Startup helper
# ---------------------------------------------------------------------------

async def apply_anti_ban_to_all_instances() -> None:
    """
    Called once at startup.
    Pushes anti-ban settings to every non-banned instance and records initial health.
    """
    instances = await _load_active_instances()
    if not instances:
        logger.info("[HealthMonitor] No instances to configure at startup")
        return

    for inst in instances:
        state = await get_state_instance(inst.instance_id, inst.api_token)
        if state == "authorized":
            await set_anti_ban_settings(inst.instance_id, inst.api_token)
            await _update_health(inst.instance_id, "authorized", is_active=True, is_banned=False)
            logger.info(f"[HealthMonitor] Startup: anti-ban applied to {inst.instance_id}")
        else:
            logger.warning(f"[HealthMonitor] Startup: {inst.instance_id} state={state} — skipping settings")
            await _update_health(inst.instance_id, state, is_active=(state not in ("blocked", "notAuthorized")), is_banned=(state == "blocked"))


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

async def run_health_monitor(stop_event: asyncio.Event) -> None:
    logger.info(f"[HealthMonitor] Started — polling every {POLL_INTERVAL_SEC}s")

    # Apply settings on startup before the first sleep
    try:
        await apply_anti_ban_to_all_instances()
    except Exception as e:
        logger.error(f"[HealthMonitor] Startup apply failed: {e}")

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                asyncio.shield(stop_event.wait()),
                timeout=POLL_INTERVAL_SEC,
            )
        except asyncio.TimeoutError:
            pass  # normal — proceed to poll

        if stop_event.is_set():
            break

        try:
            instances = await _load_active_instances()
            # Check each instance concurrently (up to 5 at a time)
            sem = asyncio.Semaphore(5)

            async def _guarded(inst: WhatsAppInstance) -> None:
                async with sem:
                    try:
                        await check_instance(inst)
                    except Exception as exc:
                        logger.error(f"[HealthMonitor] check_instance({inst.instance_id}) error: {exc}")

            await asyncio.gather(*[_guarded(i) for i in instances])
        except Exception as e:
            logger.error(f"[HealthMonitor] Poll cycle error: {e}")

    logger.info("[HealthMonitor] Stopped")
