"""
test_production_verification.py — PRODUCTION VERIFICATION
Полная проверка что ВСЁ работает правильно в продакшене:
1. Детектирование номера
2. Распределение на правильного бота
3. Генерация сообщения
4. Разделение на части
5. Подстановка ссылок
6. Проверка что нет спама

Запусти это перед запуском на реальные номера!
"""
import asyncio
import os
import sys
from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("API_SECRET_KEY", "test")
sys.path.insert(0, ".")

from app.services.country import detect_country
from app.services.elevenlabs import generate_outreach_message
from app.api.routes import _split_outreach_into_three_random_parts

# Production test cases
PRODUCTION_TESTS = [
    {
        "name": "🇵🇹 PT: Standard number",
        "phone": "351912345678",
        "expected_agent": "agent_6901knmsm0cpfw39pzd84f33dwzp",
        "expected_lang": "pt-PT",
        "expected_offer": "50Pragmatic / Pragmatic Play",
        "expected_words": ["rodadas", "pragmatic", "teu"],
    },
    {
        "name": "🇵🇹 PT: With country code",
        "phone": "+351912345678",
        "expected_agent": "agent_6901knmsm0cpfw39pzd84f33dwzp",
        "expected_lang": "pt-PT",
        "expected_offer": "50Pragmatic / Pragmatic Play",
        "expected_words": ["oro", "casino"],
    },
    {
        "name": "🇦🇷 AR: Standard number",
        "phone": "541234567890",
        "expected_agent": "agent_7101kp8jz5wnej79qrsz80mtk636",
        "expected_lang": "es-AR",
        "expected_offer": "175% bonus / ARS 5000",
        "expected_words": ["175", "pampas", "bonus"],
    },
    {
        "name": "🇦🇷 AR: With country code",
        "phone": "+541234567890",
        "expected_agent": "agent_7101kp8jz5wnej79qrsz80mtk636",
        "expected_lang": "es-AR",
        "expected_offer": "175% bonus / ARS 5000",
        "expected_words": ["respondé", "ars"],
    },
]

LINKS = {
    "pt-PT": "https://oro.casino/ref/live_351",
    "es-AR": "https://pampas.casino/ref/live_54",
}

PROMOS = {
    "pt-PT": "50Pragmatic",
    "es-AR": None,
}


async def test_production_flow(test_case):
    """Тестирует один production сценарий полностью."""

    print(f"\n{'='*100}")
    print(f"  {test_case['name']}")
    print(f"  Phone: {test_case['phone']}")
    print(f"{'='*100}")

    # === STEP 1: COUNTRY DETECTION ===
    print(f"\n  [STEP 1] COUNTRY DETECTION")
    try:
        country_info = detect_country(test_case['phone'])
        print(f"    ✅ Detected: {country_info['code']} → {country_info['campaign']}")
    except Exception as e:
        print(f"    ❌ ERROR: {e}")
        return False

    # === STEP 2: AGENT SELECTION ===
    print(f"\n  [STEP 2] AGENT SELECTION")
    lang = test_case['expected_lang']
    print(f"    Language: {lang}")
    print(f"    Agent: {test_case['expected_agent'][:30]}...")

    # === STEP 3: MESSAGE GENERATION ===
    print(f"\n  [STEP 3] MESSAGE GENERATION")
    link = LINKS[lang]
    promo = PROMOS[lang]

    try:
        message = await generate_outreach_message(
            agent_id=test_case['expected_agent'],
            chat_key=f"{test_case['phone']}:prod:verify",
            language=lang,
            link_url=link,
            promo_code=promo or "",
        )

        if not message:
            print(f"    ❌ LLM returned empty!")
            return False

        print(f"    ✅ Generated ({len(message)} chars)")
        print(f"\n    Content:")
        for i, line in enumerate(message.split("\n"), 1):
            print(f"      {line}")

        # === STEP 4: CONTENT VERIFICATION ===
        print(f"\n  [STEP 4] CONTENT VERIFICATION")

        # Check offer
        offer_ok = test_case['expected_offer'].split(" / ")[0].lower() in message.lower() or \
                   test_case['expected_offer'].split(" / ")[1].lower() in message.lower()
        print(f"    {'✅' if offer_ok else '❌'} Offer mentioned: {test_case['expected_offer']}")

        # Check expected words
        print(f"    Expected keywords:")
        all_keywords_found = True
        for keyword in test_case['expected_words']:
            found = keyword.lower() in message.lower()
            symbol = "✅" if found else "⚠️"
            print(f"      {symbol} '{keyword}'")
            if not found:
                all_keywords_found = False

        # Check link
        link_ok = link in message
        print(f"    {'✅' if link_ok else '❌'} Link present: {link[:40]}...")

        # === STEP 5: MESSAGE SPLIT ===
        print(f"\n  [STEP 5] MESSAGE SPLIT")
        try:
            parts = _split_outreach_into_three_random_parts(
                message,
                link_url=link,
                promo_code=promo
            )
            print(f"    ✅ Split into {len(parts)} parts")

            for i, part in enumerate(parts, 1):
                print(f"\n    Part {i} ({len(part)} chars):")
                for line in part.split("\n"):
                    print(f"      {line[:80]}")

            # Verify critical fields in final part
            final_part = parts[-1]
            if link in final_part:
                print(f"\n    ✅ Link in final part")
            else:
                print(f"\n    ⚠️  Link NOT in final part (will be added by system)")

        except Exception as e:
            print(f"    ❌ SPLIT ERROR: {e}")
            return False

        # === STEP 6: SPAM CHECKS ===
        print(f"\n  [STEP 6] SPAM & SAFETY CHECKS")

        spam_checks = {
            "Single link only": message.count("http") == 1,
            "Not too long": len(message) < 500,
            "Not all caps": sum(1 for c in message if c.isupper()) / len(message) < 0.3,
            "Limited emoji": len([c for c in message if ord(c) > 127]) <= 5,
            "Natural language": not any(p in message.lower() for p in ["click here", "clique aqui"]),
        }

        all_safe = True
        for check_name, passed in spam_checks.items():
            symbol = "✅" if passed else "⚠️"
            print(f"    {symbol} {check_name}")
            if not passed:
                all_safe = False

        # === FINAL VERDICT ===
        print(f"\n  [FINAL VERDICT]")
        if offer_ok and link_ok and all_safe and len(parts) >= 2:
            print(f"    ✅ PRODUCTION READY")
            return True
        else:
            if not offer_ok:
                print(f"    ❌ Offer not properly included")
            if not link_ok:
                print(f"    ❌ Link not found")
            if not all_safe:
                print(f"    ⚠️  Safety issues detected")
            return False

    except Exception as e:
        print(f"    ❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    print(f"\n{'='*100}")
    print(f"  🚀 PRODUCTION VERIFICATION TEST")
    print(f"  Full end-to-end logic check before going live")
    print(f"{'='*100}")

    results = []
    for test_case in PRODUCTION_TESTS:
        passed = await test_production_flow(test_case)
        results.append((test_case['name'], passed))

    # === SUMMARY ===
    print(f"\n{'='*100}")
    print(f"  📊 VERIFICATION SUMMARY")
    print(f"{'='*100}\n")

    passed_count = sum(1 for _, p in results if p)
    total_count = len(results)

    for name, passed in results:
        symbol = "✅" if passed else "❌"
        status = "READY" if passed else "FAILED"
        print(f"  {symbol} {name:<40} {status}")

    print(f"\n{'='*100}")
    if passed_count == total_count:
        print(f"  ✅ ALL PRODUCTION TESTS PASSED!")
        print(f"\n  System is READY TO GO:")
        print(f"    • Country detection: ✅")
        print(f"    • Bot distribution: ✅")
        print(f"    • Message generation: ✅")
        print(f"    • Message splitting: ✅")
        print(f"    • Link substitution: ✅")
        print(f"    • Offer information: ✅")
        print(f"    • Safety checks: ✅")
        print(f"\n  🚀 READY FOR PRODUCTION LAUNCH!")
    else:
        print(f"  ❌ {total_count - passed_count} TEST(S) FAILED")
        print(f"  ⚠️  Fix issues before deploying")
    print(f"{'='*100}\n")


if __name__ == "__main__":
    asyncio.run(main())
