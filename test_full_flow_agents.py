"""
test_full_flow_agents.py — Полный тест с правильными agent_id:
- agent_6901knmsm0cpfw39pzd84f33dwzp для PT (Oro/Camila)
- agent_7101kp8jz5wnej79qrsz80mtk636 для AR (Pampas/Olivia)

Проверяет:
1. Генерация сообщений на каждом агенте
2. Разделение на 3 части
3. Подстановка ссылок и промо
4. Рандомизация (разные openings в 3 отправках)
5. Что будет отправлено в Green API
"""
import asyncio
import os
import sys
from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
os.environ.setdefault("API_SECRET_KEY", "test")
os.environ.setdefault("APP_HOST", "0.0.0.0")
os.environ.setdefault("APP_PORT", "8000")

sys.path.insert(0, ".")

from app.services.elevenlabs import generate_outreach_message, _get_session
from app.api.routes import _split_outreach_into_three_random_parts

AGENT_ID_PT = "agent_6901knmsm0cpfw39pzd84f33dwzp"  # Oro/Camila
AGENT_ID_AR = "agent_7101kp8jz5wnej79qrsz80mtk636"  # Pampas/Olivia

LINK_PT  = "https://oro.casino/ref/test-pt-001"
PROMO_PT = "50Pragmatic"

LINK_AR  = "https://pampas.casino/ref/test-ar-001"
PROMO_AR = None

SEP  = "─" * 80
SEP2 = "═" * 80

async def test_lang(agent_id: str, lang: str, link_url: str, promo_code: str, n: int = 3):
    """Генерирует n сообщений, проверяет разделение и рандомизацию."""

    lang_name = "Portuguese (Oro/Camila)" if lang == "pt-PT" else "Spanish AR (Pampas/Olivia)"
    print(f"\n{SEP2}")
    print(f"  ТЕСТ: {lang_name}")
    print(f"  Agent: {agent_id}")
    print(f"  Ссылка: {link_url}")
    print(f"  Промо: {promo_code or 'None'}")
    print(f"  Отправок: {n}")
    print(SEP2)

    results = []
    openings = []

    for attempt in range(1, n + 1):
        print(f"\n{SEP}")
        print(f"  Итерация #{attempt}/{n}")
        print(SEP)

        chat_key = f"{lang}:test:{attempt}"

        try:
            # Генерируем сообщение
            print(f"  [1] Генерирую сообщение...")
            raw = await generate_outreach_message(
                agent_id=agent_id,
                chat_key=chat_key,
                language=lang,
                link_url=link_url,
                promo_code=promo_code or "",
            )

            if not raw:
                print(f"    ❌ Пусто от агента!")
                continue

            print(f"    ✓ Получено ({len(raw)} символов)")
            print(f"    └─ {raw[:120]}...")

            # Разделяем на 3 части
            print(f"\n  [2] Разделяю на 3 части...")
            parts = _split_outreach_into_three_random_parts(
                raw,
                link_url=link_url,
                promo_code=promo_code
            )
            print(f"    ✓ Разделено на {len(parts)} части")

            for i, part in enumerate(parts, 1):
                print(f"\n    ┌─ Часть {i} ({len(part)} символов):")
                for line in part.split("\n"):
                    print(f"    │  {line}")
                print(f"    └─")

                # Проверяем, что ссылка и промо на месте в последней части
                if i == len(parts):
                    if link_url not in part:
                        print(f"      ⚠️  ЛинК НЕ НАЙДЕНА в последней части!")
                    else:
                        print(f"      ✓ Ссылка найдена")
                    if promo_code and promo_code not in part:
                        print(f"      ⚠️  ПРОМО НЕ НАЙДЕНО в последней части!")
                    elif promo_code:
                        print(f"      ✓ Промо найдено")

            # Записываем opening для анализа рандомизации
            opening = parts[0].split(".")[0] if parts else ""
            openings.append(opening[:50])

            results.append({
                "attempt": attempt,
                "raw": raw,
                "parts": parts,
                "opening": opening[:50]
            })

        except Exception as e:
            print(f"    ❌ ОШИБКА: {e}")
            import traceback
            traceback.print_exc()

    # Проверяем рандомизацию
    print(f"\n{SEP}")
    print(f"  [3] АНАЛИЗ РАНДОМИЗАЦИИ")
    print(SEP)

    unique_openings = set(openings)
    print(f"  Уникальные openings: {len(unique_openings)}/{len(openings)}")
    for i, opening in enumerate(unique_openings, 1):
        count = openings.count(opening)
        marker = "✓" if count == 1 else f"⚠️  ({count}×)"
        print(f"    {marker} {opening}")

    if len(unique_openings) == len(openings):
        print(f"\n  ✅ ХОРОШО: Все сообщения уникальны по opening")
    else:
        print(f"\n  ⚠️  ВНИМАНИЕ: Повторяющиеся openings")

    return results


async def main():
    print(f"\n{'='*80}")
    print(f"  ПОЛНЫЙ ТЕСТ ДВУХ АГЕНТОВ")
    print(f"  Oro Casino (PT) + Pampas (AR) + Green API")
    print(f"{'='*80}")

    # Тест PT
    pt_results = await test_lang(
        agent_id=AGENT_ID_PT,
        lang="pt-PT",
        link_url=LINK_PT,
        promo_code=PROMO_PT,
        n=3
    )

    # Тест AR
    ar_results = await test_lang(
        agent_id=AGENT_ID_AR,
        lang="es-AR",
        link_url=LINK_AR,
        promo_code=PROMO_AR,
        n=3
    )

    # Итоговый отчет
    print(f"\n{SEP2}")
    print(f"  ИТОГОВЫЙ ОТЧЕТ")
    print(SEP2)

    print(f"\n  Portuguese (PT):")
    print(f"    Успешных генераций: {len(pt_results)}/3")
    if pt_results:
        print(f"    ✓ Ссылка подставляется: {LINK_PT in pt_results[0]['raw']}")
        print(f"    ✓ Промо подставляется: {PROMO_PT in pt_results[0]['raw']}")
        print(f"    ✓ Разделяется на части: {len(pt_results[0]['parts'])} частей")

    print(f"\n  Spanish AR:")
    print(f"    Успешных генераций: {len(ar_results)}/3")
    if ar_results:
        print(f"    ✓ Ссылка подставляется: {LINK_AR in ar_results[0]['raw']}")
        print(f"    ✓ Разделяется на части: {len(ar_results[0]['parts'])} частей")

    print(f"\n{'='*80}")
    if len(pt_results) == 3 and len(ar_results) == 3:
        print(f"  ✅ ГОТОВО К PRODUCTION!")
    else:
        print(f"  ⚠️  ТРЕБУЕТ ВНИМАНИЯ — не все сообщения сгенерировались")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    asyncio.run(main())
