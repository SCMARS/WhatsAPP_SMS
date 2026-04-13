"""
Green API control methods for WhatsApp instance health and anti-ban management.

All functions are standalone async helpers — they do NOT touch the database.
Callers (health_monitor.py, routes.py, sender.py) are responsible for DB updates.
"""

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

GREEN_API_BASE = "https://7107.api.greenapi.com"
_BASE = GREEN_API_BASE  # keep for internal use
_TIMEOUT = 15.0


def _url(instance_id: str, api_token: str, method: str) -> str:
    return f"{GREEN_API_BASE}/waInstance{instance_id}/{method}/{api_token}"


def build_url(instance_id: str, api_token: str, method: str) -> str:
    """Public helper — build a full Green API endpoint URL."""
    return _url(instance_id, api_token, method)


# ---------------------------------------------------------------------------
# State / status
# ---------------------------------------------------------------------------

async def get_state_instance(instance_id: str, api_token: str) -> str:
    """
    Return the current Green API state string for this instance.

    Possible values:
      authorized       — working normally
      notAuthorized    — QR re-scan needed
      blocked          — account banned by WhatsApp
      yellowCard       — warning, needs reboot
      sleepMode        — instance asleep (can be woken)
      unknown          — API unreachable or unexpected response
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_url(instance_id, api_token, "getStateInstance"))
        if resp.status_code == 200:
            data = resp.json()
            state = data.get("stateInstance", "unknown")
            logger.debug(f"Instance {instance_id} state={state}")
            return state
    except Exception as e:
        logger.warning(f"get_state_instance({instance_id}) failed: {e}")
    return "unknown"


async def get_status_instance(instance_id: str, api_token: str) -> dict[str, Any]:
    """
    Return the full status object from Green API (statusInstance endpoint).
    Includes 'statusInstance', 'wid', etc.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_url(instance_id, api_token, "getStatusInstance"))
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.warning(f"get_status_instance({instance_id}) failed: {e}")
    return {}


async def get_settings(instance_id: str, api_token: str) -> dict[str, Any]:
    """Return the full settings dict for this instance."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_url(instance_id, api_token, "getSettings"))
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.warning(f"get_settings({instance_id}) failed: {e}")
    return {}


# ---------------------------------------------------------------------------
# Anti-ban settings
# ---------------------------------------------------------------------------

async def set_anti_ban_settings(instance_id: str, api_token: str) -> bool:
    """
    Push recommended anti-ban settings to the instance:
      - delaySendMessagesMilliseconds = 15000  (15 s minimum between messages)
      - markIncomingMessagesReaded    = "yes"  (read receipts look human)
      - markIncomingMessagesReadedOnReply = "yes"

    Returns True if the API accepted the change (200 OK).
    """
    payload = {
        "delaySendMessagesMilliseconds": 15000,
        "markIncomingMessagesReaded": "yes",
        "markIncomingMessagesReadedOnReply": "yes",
        "keepOnlineStatus": "yes",   # keep instance visible as "online" — looks human
        "stateWebhook": "yes",       # real-time ban/yellowCard notifications via webhook
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _url(instance_id, api_token, "setSettings"),
                json=payload,
            )
        ok = resp.status_code == 200
        if ok:
            logger.info(f"Anti-ban settings applied to instance {instance_id}")
        else:
            logger.warning(
                f"set_anti_ban_settings({instance_id}) returned {resp.status_code}: {resp.text[:200]}"
            )
        return ok
    except Exception as e:
        logger.warning(f"set_anti_ban_settings({instance_id}) failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Phone number verification
# ---------------------------------------------------------------------------

async def check_whatsapp(instance_id: str, api_token: str, phone: str) -> bool:
    """
    Check whether a phone number is registered on WhatsApp.

    `phone` should be digits only (e.g. "380671234567").
    Returns True if the number has WhatsApp, False otherwise (or on error — fail-open
    so a network blip doesn't block all sends).

    NOTE: Green API warns against calling this too frequently.
    Callers must cache results (see sender.py _whatsapp_cache).
    """
    try:
        phone_int = int("".join(c for c in phone if c.isdigit()))
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _url(instance_id, api_token, "checkWhatsapp"),
                json={"phoneNumber": phone_int},
            )
        if resp.status_code == 200:
            exists = resp.json().get("existsWhatsapp", True)
            logger.debug(f"checkWhatsapp({phone}) → existsWhatsapp={exists}")
            return bool(exists)
    except Exception as e:
        logger.warning(f"check_whatsapp({phone}) failed: {e}")
    # Fail-open: if the check errors, allow the send to proceed
    return True


# ---------------------------------------------------------------------------
# Instance lifecycle
# ---------------------------------------------------------------------------

async def reboot_instance(instance_id: str, api_token: str) -> bool:
    """
    Reboot the Green API instance (recommended after yellowCard).
    Returns True if accepted.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_url(instance_id, api_token, "reboot"))
        ok = resp.status_code == 200
        if ok:
            logger.info(f"Instance {instance_id} rebooted")
        else:
            logger.warning(f"reboot_instance({instance_id}) returned {resp.status_code}")
        return ok
    except Exception as e:
        logger.warning(f"reboot_instance({instance_id}) failed: {e}")
        return False


async def unban_instance(instance_id: str, api_token: str) -> bool:
    """
    Attempt to unban a blocked instance via Green API's unban endpoint.
    This is NOT guaranteed to succeed — WhatsApp bans are server-side.
    Returns True if the API call itself succeeded (not that the ban was lifted).
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_url(instance_id, api_token, "unban"))
        ok = resp.status_code == 200
        if ok:
            logger.info(f"Unban requested for instance {instance_id}")
        else:
            logger.warning(f"unban_instance({instance_id}) returned {resp.status_code}")
        return ok
    except Exception as e:
        logger.warning(f"unban_instance({instance_id}) failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Message queue
# ---------------------------------------------------------------------------

async def show_messages_queue(instance_id: str, api_token: str) -> list[dict]:
    """Return the list of pending queued messages for this instance."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_url(instance_id, api_token, "showMessagesQueue"))
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"show_messages_queue({instance_id}) failed: {e}")
    return []


async def clear_messages_queue(instance_id: str, api_token: str) -> bool:
    """
    Clear the pending message queue.
    Call this after a reboot so stale messages don't fire in a burst.
    Returns True if accepted.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_url(instance_id, api_token, "clearMessagesQueue"))
        ok = resp.status_code == 200
        if ok:
            logger.info(f"Message queue cleared for instance {instance_id}")
        else:
            logger.warning(f"clear_messages_queue({instance_id}) returned {resp.status_code}")
        return ok
    except Exception as e:
        logger.warning(f"clear_messages_queue({instance_id}) failed: {e}")
        return False
