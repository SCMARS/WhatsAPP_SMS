"""
Quick tests for WR WhatsApp Service.

Run with: python test_quick.py

Tests:
  1. Green API — send a test message
  2. ElevenLabs — get agent prompt
  3. ElevenLabs — generate a text reply
"""

import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
INSTANCE_ID = os.getenv("INSTANCE_ID", "")
INSTANCE_API_TOKEN = os.getenv("INSTANCE_API_TOKEN", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
AGENT_ID = "agent_6401kmt60076f7h9d6jtmn6hsg1f"  # Riley - Golden Reels (text-only)
TEST_PHONE = "380671202709"

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"
GREEN_API_BASE = "https://7107.api.greenapi.com"


def check_env():
    missing = []
    if not INSTANCE_ID:
        missing.append("INSTANCE_ID")
    if not INSTANCE_API_TOKEN or INSTANCE_API_TOKEN == "your_instance_api_token_here":
        missing.append("INSTANCE_API_TOKEN")
    if not ELEVENLABS_API_KEY:
        missing.append("ELEVENLABS_API_KEY")
    if missing:
        print(f"[WARN] Missing or placeholder env vars: {', '.join(missing)}")
        print("       Set them in .env before running full tests.\n")
    return missing


# ---------------------------------------------------------------------------
# Test 1: Green API send message
# ---------------------------------------------------------------------------
async def test_green_api_send():
    print("=" * 60)
    print("TEST 1: Green API — Send Message")
    print("=" * 60)

    if not INSTANCE_ID or not INSTANCE_API_TOKEN or INSTANCE_API_TOKEN == "your_instance_api_token_here":
        print("[SKIP] INSTANCE_ID or INSTANCE_API_TOKEN not configured\n")
        return False

    url = f"{GREEN_API_BASE}/waInstance{INSTANCE_ID}/sendMessage/{INSTANCE_API_TOKEN}"
    chat_id = f"{TEST_PHONE}@c.us"
    body = {"chatId": chat_id, "message": "Тест WR WhatsApp сервиса"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=body)

    print(f"  Status: {resp.status_code}")
    try:
        data = resp.json()
        print(f"  Response: {data}")
        if resp.status_code == 200 and "idMessage" in data:
            print(f"  [OK] Message sent! idMessage={data['idMessage']}\n")
            return True
        else:
            print(f"  [FAIL] Unexpected response\n")
            return False
    except Exception as e:
        print(f"  [FAIL] Could not parse response: {e}\n")
        return False


# ---------------------------------------------------------------------------
# Test 2: ElevenLabs — Get agent prompt
# ---------------------------------------------------------------------------
async def test_elevenlabs_get_prompt():
    print("=" * 60)
    print("TEST 2: ElevenLabs — Get Agent Prompt")
    print("=" * 60)

    if not ELEVENLABS_API_KEY:
        print("[SKIP] ELEVENLABS_API_KEY not configured\n")
        return False, None

    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    url = f"{ELEVENLABS_BASE}/convai/agents/{AGENT_ID}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=headers)

    print(f"  Status: {resp.status_code}")

    if resp.status_code != 200:
        print(f"  [FAIL] HTTP {resp.status_code}: {resp.text[:200]}\n")
        return False, None

    data = resp.json()
    try:
        agent_cfg = data["conversation_config"]["agent"]
        llm_model = data["conversation_config"].get("tts", {}).get("model_id", "unknown")
        first_message = agent_cfg.get("first_message", "")
        prompt = agent_cfg["prompt"]["prompt"]

        print(f"  LLM model   : {llm_model}")
        print(f"  First message: {first_message[:100] if first_message else '(none)'}")
        print(f"  Prompt (200): {prompt[:200]}...")
        print(f"  [OK] Agent prompt retrieved\n")
        return True, prompt
    except (KeyError, TypeError) as e:
        print(f"  [FAIL] Could not parse agent config: {e}")
        print(f"  Keys: {list(data.keys())}\n")
        return False, None


# ---------------------------------------------------------------------------
# Test 3: ElevenLabs — Generate text reply
# ---------------------------------------------------------------------------
async def test_elevenlabs_generate(system_prompt: str):
    print("=" * 60)
    print("TEST 3: ElevenLabs — Generate Text Reply (WebSocket)")
    print("=" * 60)

    if not ELEVENLABS_API_KEY:
        print("[SKIP] ELEVENLABS_API_KEY not configured\n")
        return False

    import sys
    sys.path.insert(0, ".")
    os.environ["ELEVENLABS_API_KEY"] = ELEVENLABS_API_KEY
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://wrwa:wrwa_secret@localhost:5433/wr_whatsapp")
    os.environ.setdefault("API_SECRET_KEY", "wr-whatsapp-secret-2026")

    from app.services.elevenlabs import generate_text_reply

    history = [{"role": "user", "content": "Привет, кто ты?"}]

    try:
        reply = await generate_text_reply(
            agent_id=AGENT_ID,
            system_prompt=system_prompt or "",
            history=history,
            lead_name="Тест",
        )
        if reply:
            print(f"  Reply: {reply}")
            print(f"  [OK] Got reply from ElevenLabs agent via WebSocket\n")
            return True
        else:
            print(f"  [FAIL] Empty reply\n")
            return False
    except Exception as e:
        print(f"  [FAIL] Exception: {e}\n")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    print("\nWR WhatsApp Service — Quick Tests")
    print("=" * 60)

    missing = check_env()

    results = {}

    results["green_api"] = await test_green_api_send()

    ok2, prompt = await test_elevenlabs_get_prompt()
    results["elevenlabs_prompt"] = ok2

    results["elevenlabs_generate"] = await test_elevenlabs_generate(prompt or "")

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for name, ok in results.items():
        icon = "[OK]" if ok else "[FAIL/SKIP]"
        print(f"  {icon}  {name}")
    print()

    failed = [k for k, v in results.items() if not v]
    if failed:
        print(f"Failed/skipped: {', '.join(failed)}")
        sys.exit(1)
    else:
        print("All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
