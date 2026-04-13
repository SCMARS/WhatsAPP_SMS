"""
Google Gemini Vision — анализ изображений из WhatsApp.
Скачивает фото и описывает его, чтобы передать описание агенту как контекст.
"""
import base64
import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models"
    "/gemini-2.0-flash:generateContent"
)

IMAGE_PROMPT = (
    "Describe this image briefly in 1-2 sentences as if you are telling "
    "a sales agent what the customer sent. Focus on what is shown. "
    "Reply in the same language the image text uses, or in Russian if no text."
)


async def _download_image(
    url: str,
    instance_id: Optional[str] = None,
    api_token: Optional[str] = None,
    message_id: Optional[str] = None,
) -> Optional[bytes]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(url)
            if r.status_code == 200 and r.content:
                return r.content
            logger.warning(f"Direct image download returned {r.status_code}")
        except Exception as e:
            logger.warning(f"Direct image download failed: {e}")

        if instance_id and api_token and message_id:
            try:
                from app.services.green_api import build_url
                r = await client.post(
                    build_url(instance_id, api_token, "downloadFile"),
                    json={"idMessage": message_id},
                )
                if r.status_code == 200:
                    file_b64 = r.json().get("body", "")
                    if file_b64:
                        return base64.b64decode(file_b64)
            except Exception as e:
                logger.error(f"Green API image download failed: {e}")

    return None


async def describe_image(
    image_url: str,
    instance_id: Optional[str] = None,
    api_token: Optional[str] = None,
    message_id: Optional[str] = None,
    caption: Optional[str] = None,
) -> str:
    """
    Download image and describe it via Gemini Vision.
    Returns a text description to pass as context to the agent.
    Falls back to placeholder if Gemini key is missing or download fails.
    """
    if not settings.GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set, using placeholder for image")
        if caption:
            return f"[The customer sent a photo. Caption: {caption}]"
        return "[The customer sent a photo]"

    image_bytes = await _download_image(image_url, instance_id, api_token, message_id)
    if not image_bytes:
        logger.error("Could not download image from any source")
        if caption:
            return f"[The customer sent a photo (could not download). Caption: {caption}]"
        return "[The customer sent a photo that could not be downloaded]"

    # Detect mime type from magic bytes
    mime_type = "image/jpeg"
    if image_bytes[:4] == b"\x89PNG":
        mime_type = "image/png"
    elif image_bytes[:4] == b"RIFF":
        mime_type = "image/webp"

    b64_image = base64.b64encode(image_bytes).decode()

    prompt_parts = [{"text": IMAGE_PROMPT}]
    if caption:
        prompt_parts.append({"text": f"Caption from the customer: {caption}"})
    prompt_parts.append({
        "inline_data": {"mime_type": mime_type, "data": b64_image}
    })

    payload = {"contents": [{"parts": prompt_parts}]}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                GEMINI_API_URL,
                params={"key": settings.GEMINI_API_KEY},
                json=payload,
            )
            resp.raise_for_status()
            description = (
                resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            )
        logger.info(f"Gemini image description: {description[:120]}")
        return f"[Photo from customer: {description}]"

    except Exception as e:
        logger.error(f"Gemini Vision failed: {e}")
        return "[The customer sent a photo]"
