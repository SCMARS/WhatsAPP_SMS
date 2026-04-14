"""
test_real_flow.py — тест через НАСТОЯЩИЙ generate_outreach_message + split
ровно так, как это происходит в production (/api/send/bulk).
"""
import asyncio
import os
import sys
import re

from dotenv import load_dotenv
load_dotenv()
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
os.environ.setdefault("API_SECRET_KEY", "test")
os.environ.setdefault("APP_HOST", "0.0.0.0")
os.environ.setdefault("APP_PORT", "8000")

sys.path.insert(0, ".")

from app.services.elevenlabs import generate_outreach_message
from app.api.routes import _split_outreach_into_three_random_parts

AGENT_ID = os.environ["AGENT_ID"]
LINK_PT  = "https://oro.casino/ref/testPT"
LINK_AR  = "https://pampas.casino/ref/testAR"
PROMO_PT = "PROMO50"
PROMO_AR = None

SEP = "─" * 70

async def run_lang(lang: str, link: str, promo, n=5):
    print(f"\n{'═'*70}")
    print(f"  ЯЗЫК: {lang}  ({n} итераций)")
    print('═'*70)
    raw_messages = []
    for i in range(1, n + 1):
        chat_key = f"test_{lang}_{i:03d}"
        print(f"\n{SEP}")
        print(f"  Итерация #{i} | chat_key={chat_key}")
        print(SEP)
        raw = await generate_outreach_message(
            agent_id=AGENT_ID,
            chat_key=chat_key,
            language=lang,
            link_url=link,
            promo_code=promo or "",
        )
        print(f"  [RAW из ElevenLabs]:")
        for line in (raw or "(ПУСТО)").splitlines():
            print(f"    {line}")

        # Exactly what _resolve_initial_message does:
        parts = _split_outreach_into_three_random_parts(raw)
        print(f"\n  [ПОСЛЕ SPLIT — {len(parts)} частей, это то что идёт в Green API/TG]:")
        for j, p in enumerate(parts, 1):
            print(f"    Часть {j}: «{p}»")

        raw_messages.append(raw)

    print(f"\n{'═'*70}")
    print(f"  ИТОГ уникальности [{lang}]")
    print('═'*70)

    def opening6(t):
        cleaned = re.sub(r"[^\w\s]", "", (t or "").lower())
        return " ".join(cleaned.split()[:6])

    openings = [opening6(m) for m in raw_messages]
    unique = len(set(openings))
    print(f"  Уникальных открывающих фраз: {unique}/{n}")
    dup_parts = sum(1 for m in raw_messages
                    if len(set(_split_outreach_into_three_random_parts(m))) < 3)
    print(f"  Сообщений с дублирующимися частями: {dup_parts}/{n}")


async def main():
    await run_lang("pt-PT", LINK_PT, PROMO_PT, 5)
    await run_lang("es-AR", LINK_AR, PROMO_AR, 5)


if __name__ == "__main__":
    asyncio.run(main())
