"""
show_full_pipeline.py — Показывает что КОНКРЕТНО генерит LLM и что уходит в WhatsApp.
"""
import asyncio
import os
import sys
import random
from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("API_SECRET_KEY", "test")
sys.path.insert(0, ".")

from app.services.elevenlabs import generate_outreach_message, build_outreach_parts
from app.services.rate_limiter import insert_zero_width, calc_typing_time

CONFIGS = [
    {
        "name": "🇵🇹 PORTUGAL (Oro / Camila)",
        "agent_id": "agent_6901knmsm0cpfw39pzd84f33dwzp",
        "lang": "pt-PT",
        "link": "https://oro.casino/ref/live_351",
        "promo": "50Pragmatic",
    },
    {
        "name": "🇦🇷 ARGENTINA (Pampas / Olivia)",
        "agent_id": "agent_7101kp8jz5wnej79qrsz80mtk636",
        "lang": "es-AR",
        "link": "https://pampas.casino/ref/live_54",
        "promo": None,
    },
]


def show_message(text: str, part_num: int, delay: float):
    width = 72
    print(f"  ⏱️  Пауза: ~{delay:.0f} сек  |  ✏️ Typing: {calc_typing_time(text)/1000:.1f} сек")
    print(f"  📤 Сообщение {part_num}:")
    print(f"  ┌{'─' * width}┐")
    for line in text.split("\n"):
        while len(line) > width - 2:
            print(f"  │ {line[:width-2].ljust(width-2)} │")
            line = line[width-2:]
        print(f"  │ {line.ljust(width-2)} │")
    print(f"  └{'─' * width}┘")
    print()


async def demo(config: dict):
    print(f"\n{'=' * 78}")
    print(f"  {config['name']}")
    print(f"{'=' * 78}")

    print(f"\n  🤖 Генерация через LLM (ElevenLabs ConvAI)...")
    parts = await generate_outreach_message(
        agent_id=config["agent_id"],
        chat_key=f"pipeline-demo-{config['lang']}",
        language=config["lang"],
        link_url=config["link"],
        promo_code=config.get("promo") or "",
    )

    if not parts:
        print(f"  ❌ LLM вернул пусто, используем шаблон")
        parts = build_outreach_parts(config["lang"], config["link"], config.get("promo"))

    source = "LLM" if len(parts) == 3 else "FALLBACK"
    print(f"\n  ✅ Источник: {source} | Частей: {len(parts)}")
    print(f"\n  {'─' * 72}")
    print(f"  ЧТО ВИДИТ ЮЗЕР В WHATSAPP:")
    print(f"  {'─' * 72}\n")

    delays = [random.uniform(18, 38)] + [random.uniform(15, 35) for _ in range(len(parts) - 1)]

    for i, part in enumerate(parts):
        final = insert_zero_width(part)
        show_message(final, i + 1, delays[i])

    total = sum(delays) + sum(calc_typing_time(p) / 1000 for p in parts)
    print(f"  📊 Всего: {len(parts)} сообщений, ~{total:.0f} сек ({total/60:.1f} мин)\n")


async def main():
    print(f"\n{'=' * 78}")
    print(f"  📱 LLM ГЕНЕРАЦИЯ → 3 СООБЩЕНИЯ В WHATSAPP")
    print(f"{'=' * 78}")

    for config in CONFIGS:
        await demo(config)

    print(f"\n{'=' * 78}")
    print(f"  ✅ ГОТОВО")
    print(f"{'=' * 78}\n")


if __name__ == "__main__":
    asyncio.run(main())
