"""
test_check_generation.py — Проверка ЧТО ГЕНЕРИРУЕТ агент:
1. Генерирует 5 реальных сообщений от каждого агента
2. Анализирует их на:
   - Правильность языка
   - Отсутствие спама-сигналов
   - Бренд-безопасность
   - Риск блокировки
   - Подозрительные паттерны
"""
import asyncio
import os
import sys
from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("API_SECRET_KEY", "test")
sys.path.insert(0, ".")

from app.services.elevenlabs import generate_outreach_message

AGENT_ID_PT = "agent_6901knmsm0cpfw39pzd84f33dwzp"
AGENT_ID_AR = "agent_7101kp8jz5wnej79qrsz80mtk636"

LINK_PT = "https://oro.casino/ref/live_351"
PROMO_PT = "50Pragmatic"

LINK_AR = "https://pampas.casino/ref/live_54"
PROMO_AR = None

# Флаги спама
SPAM_PATTERNS_PT = {
    "🔴 Слишком много ссылок": lambda x: x.count("http") > 1,
    "🔴 Слишком много символов": lambda x: len(x) > 500,
    "🔴 Капс больше 30%": lambda x: sum(1 for c in x if c.isupper()) / len(x) > 0.3,
    "🔴 Много emoji": lambda x: len([c for c in x if ord(c) > 127]) > 10,
    "🔴 Repeated chars (HELLLLLLО)": lambda x: any(c*4 in x for c in "абвгдежзийклмнопрстуфхцчшщъыьэюя"),
    "🔴 'Click here' pattern": lambda x: any(p in x.lower() for p in ["clique aqui", "clickear aqui"]),
}

SPAM_PATTERNS_AR = {
    "🔴 Слишком много ссылок": lambda x: x.count("http") > 1,
    "🔴 Слишком много символов": lambda x: len(x) > 500,
    "🔴 Капс больше 30%": lambda x: sum(1 for c in x if c.isupper()) / max(1, len(x)) > 0.3,
    "🔴 Много emoji": lambda x: len([c for c in x if ord(c) > 127]) > 10,
    "🔴 Repeated chars": lambda x: any(c*4 in x for c in "abcdefghijklmnopqrstuvwxyz"),
}

REQUIRED_PT = {
    "✅ Есть 'teu' или 'tua'": lambda x: any(w in x.lower() for w in ["teu", "tua"]),
    "✅ Есть 'rodadas' или 'free spins'": lambda x: any(w in x.lower() for w in ["rodadas", "free spins"]),
    "✅ Естественный тон (не корпоративный)": lambda x: not any(p in x.lower() for p in ["уважаемый", "договор", "условия"]),
    "✅ Ссылка присутствует": lambda x: "http" in x,
}

REQUIRED_AR = {
    "✅ Есть 'vos' или восео-глаголы": lambda x: any(w in x.lower() for w in ["vos", "respondé", "mandás", "activá"]),
    "✅ Есть '175%' или 'bono'": lambda x: any(w in x for w in ["175%", "175", "bono", "bonus"]),
    "✅ Естественный тон": lambda x: not any(p in x.lower() for p in ["estimado", "contrato", "términos"]),
    "✅ Ссылка присутствует": lambda x: "http" in x,
}

async def analyze_message(msg, lang="pt-PT"):
    """Анализирует сообщение на риски и качество."""

    required = REQUIRED_PT if lang == "pt-PT" else REQUIRED_AR
    spam_patterns = SPAM_PATTERNS_PT if lang == "pt-PT" else SPAM_PATTERNS_AR

    print(f"\n    📝 Текст ({len(msg)} символов):")
    for line in msg.split("\n"):
        print(f"       {line}")

    print(f"\n    🔍 Анализ качества:")

    # Проверяем required поля
    passed_required = 0
    for check_name, check_func in required.items():
        try:
            passed = check_func(msg)
            symbol = "✅" if passed else "⚠️"
            print(f"       {symbol} {check_name}")
            if passed:
                passed_required += 1
        except:
            print(f"       ⚠️ {check_name} (error in check)")

    # Проверяем спам-сигналы
    print(f"\n    ⚠️ Риск-флаги:")
    has_spam = False
    for check_name, check_func in spam_patterns.items():
        try:
            triggered = check_func(msg)
            if triggered:
                print(f"       {check_name}")
                has_spam = True
        except:
            pass

    if not has_spam:
        print(f"       ✅ Нет спам-сигналов")

    # Общая оценка
    quality_score = (passed_required / len(required)) * 100

    if quality_score >= 75 and not has_spam:
        status = "🟢 SAFE (низкий риск блокировки)"
    elif quality_score >= 50:
        status = "🟡 CAUTION (средний риск)"
    else:
        status = "🔴 RISK (высокий риск)"

    print(f"\n    📊 Score: {quality_score:.0f}% | Status: {status}")
    return quality_score, not has_spam


async def test_agent(agent_id, lang, link, promo, name, n=5):
    """Генерирует n сообщений и анализирует их."""

    print(f"\n{'='*100}")
    print(f"  {name}")
    print(f"  Agent: {agent_id[:25]}...")
    print(f"  Language: {lang}")
    print(f"  Link: {link}")
    print(f"  Promo: {promo or 'None'}")
    print(f"{'='*100}")

    all_messages = []
    all_safe = True
    total_score = 0

    for attempt in range(1, n + 1):
        print(f"\n  [{attempt}/{n}] Генерирую сообщение...")

        try:
            msg = await generate_outreach_message(
                agent_id=agent_id,
                chat_key=f"{lang}:analysis:{attempt}",
                language=lang,
                link_url=link,
                promo_code=promo or "",
            )

            if msg:
                all_messages.append(msg)
                score, is_safe = await analyze_message(msg, lang)
                total_score += score
                if not is_safe:
                    all_safe = False
            else:
                print(f"       ❌ Агент вернул пусто")
        except Exception as e:
            print(f"       ❌ Ошибка: {e}")

    # Итоговый отчет
    print(f"\n{'-'*100}")
    print(f"  📊 ИТОГОВЫЙ ОТЧЕТ ({n} сообщений)")
    print(f"{'-'*100}")

    if all_messages:
        avg_score = total_score / len(all_messages)
        print(f"  Average score: {avg_score:.0f}%")
        print(f"  Generated: {len(all_messages)}/{n}")

        # Проверяем уникальность
        unique = len(set(m[:50] for m in all_messages))
        print(f"  Unique openings: {unique}/{len(all_messages)}")

        if all_safe and avg_score >= 75 and unique >= len(all_messages) - 1:
            print(f"\n  ✅ PRODUCTION READY!")
            print(f"     - Все сообщения безопасны")
            print(f"     - Качество высокое")
            print(f"     - Рандомизация работает")
        else:
            if not all_safe:
                print(f"\n  ⚠️  ВНИМАНИЕ: Обнаружены спам-сигналы")
            if avg_score < 75:
                print(f"\n  ⚠️  ВНИМАНИЕ: Низкое качество ({avg_score:.0f}%)")
            if unique < len(all_messages) - 1:
                print(f"\n  ⚠️  ВНИМАНИЕ: Недостаточно уникальных openings")
    else:
        print(f"  ❌ Ошибка: сообщения не сгенерировались")


async def main():
    print(f"\n{'='*100}")
    print(f"  PRODUCTION QUALITY CHECK")
    print(f"  Анализ РЕАЛЬНЫХ сообщений от агентов")
    print(f"  Проверка спама, качества, риска блокировки")
    print(f"{'='*100}")

    await test_agent(
        agent_id=AGENT_ID_PT,
        lang="pt-PT",
        link=LINK_PT,
        promo=PROMO_PT,
        name="🇵🇹 PORTUGAL (Oro/Camila)",
        n=5
    )

    await test_agent(
        agent_id=AGENT_ID_AR,
        lang="es-AR",
        link=LINK_AR,
        promo=PROMO_AR,
        name="🇦🇷 ARGENTINA (Pampas/Olivia)",
        n=5
    )

    print(f"\n{'='*100}")
    print(f"  КОНЕЦ АНАЛИЗА")
    print(f"{'='*100}\n")


if __name__ == "__main__":
    asyncio.run(main())
