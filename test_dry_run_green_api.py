"""
test_dry_run_green_api.py — Production dry run:
Генерирует сообщения и показывает ТОЧНО что отправится в Green API

Это симуляция — не отправляет реально, только показывает что отправится.
"""
import asyncio
import sys
import json
import os
from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("API_SECRET_KEY", "test")
sys.path.insert(0, ".")
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")

# ─────────────────────────────────────────────────────────────────
# Тестовые данные — реалистичный AI-ответ как от ElevenLabs
# ─────────────────────────────────────────────────────────────────
AI_MESSAGES = [
    {
        "desc": "🇵🇹 Português — Oro Casino (с promo + link)",
        "ai_message": (
            "Olá! Tenho uma oferta exclusiva para ti hoje. "
            "Usa o teu código especial e recebe 50 Free Spins no primeiro depósito. "
            "O bónus ativa assim que fizeres o registo 🎰"
        ),
        "promo": "PROMO123",
        "link": "https://oro.casino/ref/abc123",
        "phone": "351912345678",
    },
    {
        "desc": "🇦🇷 Español — Pampas Casino (без promo, только link)",
        "ai_message": (
            "¡Hola! Hoy tenemos algo especial para ti. "
            "Recibirás 50 giros gratis con tu primer depósito. "
            "¡Es muy fácil de activar!"
        ),
        "promo": "",
        "link": "https://pampas.casino/ref/xyz789",
        "phone": "5491112345678",
    },
    {
        "desc": "🇵🇹 Português — короткий ответ AI",
        "ai_message": "Olá! Claro, o teu link de bónus já está pronto para ti.",
        "promo": "GOLD50",
        "link": "https://oro.casino/ref/short",
        "phone": "351987654321",
    },
]

# ─────────────────────────────────────────────────────────────────
# Перехваченные вызовы
# ─────────────────────────────────────────────────────────────────
captured_calls: list[dict] = []


class FakeResponse:
    status_code = 200

    def json(self):
        return {"idMessage": f"FAKE_MSG_{int(time.time()*1000)}"}


class FakeAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url: str, json: dict):
        endpoint = url.split("/")[-1]
        captured_calls.append({
            "endpoint": endpoint,
            "url": url,
            "payload": json,
        })
        return FakeResponse()


async def run_dry_run(case: dict) -> None:
    from app.services.sender import split_message, build_url, insert_zero_width, _format_phone

    ai_message = case["ai_message"]
    promo = case["promo"]
    link = case["link"]
    phone = case["phone"]

    print(f"\n{'═'*64}")
    print(f"  {case['desc']}")
    print(f"{'═'*64}")
    print(f"\n📝 AI сообщение (полное):\n   {ai_message!r}")
    print(f"   promo={promo!r}  link={link!r}\n")

    # Split
    p1, p2, p3 = split_message(ai_message, promo, link)
    parts = [p1, p2, p3]

    print("✂️  Разбивка на 3 части:")
    for i, p in enumerate(parts, 1):
        typing_secs = max(3.0, min(10.0, len(p) * 0.05))
        print(f"  Part {i}: {p!r}")
        print(f"          → typing_time={typing_secs:.1f}s  len={len(p)}")

    chat_id = f"{''.join(c for c in phone if c.isdigit())}@c.us"
    instance_id = "7107582303"
    api_token = "a64e972ee2ba4978abd66f918dbe4022ca3e0b1b99af46c7a5"

    print(f"\n🌐 Green API вызовы (dry-run, в WhatsApp НЕ отправляется):")
    print(f"   chatId = {chat_id}")

    captured_calls.clear()

    # Simulate the send_split_message loop without DB/instance pool
    import httpx

    with patch("httpx.AsyncClient", FakeAsyncClient):
        for i, part in enumerate(parts):
            typing_secs = max(3.0, min(10.0, len(part) * 0.05))
            typing_url = build_url(instance_id, api_token, "sendTyping")
            send_url = build_url(instance_id, api_token, "sendMessage")
            text_to_send = insert_zero_width(part)

            async with FakeAsyncClient() as client:
                await client.post(typing_url, json={
                    "chatId": chat_id,
                    "typingTime": int(typing_secs),
                })

            # Simulate sleep (скипаем в тесте)
            # await asyncio.sleep(typing_secs)

            async with FakeAsyncClient() as client:
                r = await client.post(send_url, json={
                    "chatId": chat_id,
                    "message": text_to_send,
                    "linkPreview": False,
                })
                fake_id = r.json()["idMessage"]

            print(f"\n  ┌─ Part {i+1}/3 ──────────────────────────────────────")
            print(f"  │ [1] POST sendTyping")
            print(f"  │     URL : {typing_url}")
            print(f"  │     Body: {json.dumps({'chatId': chat_id, 'typingTime': int(typing_secs)}, ensure_ascii=False)}")
            print(f"  │     ⏳  sleep {typing_secs:.1f}s (эмуляция печати)")
            print(f"  │")
            print(f"  │ [2] POST sendMessage")
            print(f"  │     URL : {send_url}")
            body_preview = text_to_send.replace('\n', '\\n')
            print(f"  │     Body: {json.dumps({'chatId': chat_id, 'message': body_preview, 'linkPreview': False}, ensure_ascii=False)}")
            print(f"  │     ✅  idMessage: {fake_id}")
            if i < len(parts) - 1:
                import random
                pause = random.uniform(8, 20)
                print(f"  │     ⏳  пауза {pause:.1f}s перед следующей частью")
            print(f"  └────────────────────────────────────────────────────")

    print(f"\n✅ Итого вызовов Green API: {len(captured_calls)} (sendTyping×3 + sendMessage×3)")


async def main():
    print("\n" + "█"*64)
    print("  DRY-RUN: что улетит в Green API при send_split_message()")
    print("  HTTP не отправляется — только показываем payload")
    print("█"*64)

    for case in AI_MESSAGES:
        await run_dry_run(case)

    print(f"\n\n{'═'*64}")
    print("  Все кейсы пройдены. В WhatsApp ничего не отправлено.")
    print(f"{'═'*64}\n")


if __name__ == "__main__":
    asyncio.run(main())
