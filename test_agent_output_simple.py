"""
test_agent_output_simple.py — Простой тест: что генерирует агент?
Без сплиттинга, просто raw output от ElevenLabs
"""
import asyncio
import os
import sys
from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("API_SECRET_KEY", "test")
sys.path.insert(0, ".")

from app.services.elevenlabs import generate_outreach_message

AGENT_ID_PT = "agent_6901knmsm0cpfw39pzd84f33dwzp"  # Oro/Camila
AGENT_ID_AR = "agent_7101kp8jz5wnej79qrsz80mtk636"  # Pampas/Olivia

LINK_PT  = "https://oro.casino/ref/test-pt"
PROMO_PT = "50Pragmatic"

LINK_AR  = "https://pampas.casino/ref/test-ar"
PROMO_AR = None

async def test_raw_output(agent_id: str, lang: str, link: str, promo: str, n: int = 3):
    """Генерирует n сообщений и показывает raw output."""

    lang_name = f"{'🇵🇹 PORTUGAL (Oro/Camila)' if lang == 'pt-PT' else '🇦🇷 ARGENTINA (Pampas/Olivia)'}"
    print(f"\n{'='*90}")
    print(f"  {lang_name}")
    print(f"  Agent: {agent_id}")
    print(f"  Link: {link}")
    print(f"  Promo: {promo or 'None'}")
    print(f"{'='*90}\n")

    for attempt in range(1, n + 1):
        try:
            print(f"  📨 Попытка {attempt}/{n}:")
            raw = await generate_outreach_message(
                agent_id=agent_id,
                chat_key=f"{lang}:attempt:{attempt}",
                language=lang,
                link_url=link,
                promo_code=promo or "",
            )

            if raw:
                print(f"  ✅ Получено ({len(raw)} символов):\n")
                for i, line in enumerate(raw.split("\n"), 1):
                    print(f"      {line}")

                # Проверяем наличие ключевых элементов
                checks = {
                    "✓ Ссылка подставлена": link in raw,
                }
                if promo:
                    checks["✓ Промо подставлено"] = promo in raw

                print(f"\n      Проверки:")
                for check, passed in checks.items():
                    symbol = "✓" if passed else "✗"
                    print(f"      {symbol} {check}")

                # Проверяем язык и стиль
                if lang == "pt-PT":
                    style_checks = {
                        "Есть 'rodadas'": "rodadas" in raw.lower(),
                        "Есть 'boa sorte' или 'sorte'": "sorte" in raw.lower(),
                        "Есть 'teu/tua'": any(w in raw.lower() for w in ["teu", "tua"]),
                    }
                else:  # es-AR
                    style_checks = {
                        "Есть 'vos' или 'respondé'": any(w in raw.lower() for w in ["vos", "respondé", "mandás"]),
                        "Есть '175%'": "175" in raw,
                        "Есть 'ARS' или '5000'": any(w in raw for w in ["ARS", "5000"]),
                    }

                print(f"      Стиль:")
                for check, passed in style_checks.items():
                    symbol = "✓" if passed else "⚠️"
                    print(f"      {symbol} {check}")

                print()
            else:
                print(f"  ❌ Пусто!\n")

        except Exception as e:
            print(f"  ❌ ОШИБКА: {e}\n")


async def main():
    print(f"\n{'='*90}")
    print(f"  RAW AGENT OUTPUT TEST")
    print(f"  Что ТОЧНО генерируют агенты")
    print(f"{'='*90}")

    # Тест PT
    await test_raw_output(
        agent_id=AGENT_ID_PT,
        lang="pt-PT",
        link=LINK_PT,
        promo=PROMO_PT,
        n=3
    )

    # Тест AR
    await test_raw_output(
        agent_id=AGENT_ID_AR,
        lang="es-AR",
        link=LINK_AR,
        promo=PROMO_AR,
        n=3
    )

    print(f"\n{'='*90}")
    print(f"  ✅ ГОТОВО")
    print(f"{'='*90}\n")


if __name__ == "__main__":
    asyncio.run(main())
