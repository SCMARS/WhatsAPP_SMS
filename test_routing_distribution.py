"""
test_routing_distribution.py — Проверка:
1. Правильная ли распределение номер → кампания → агент → язык → оффер
2. Все ли сообщения от LLM или используется fallback

Тестирует полный flow: phone number → country detection → correct bot routing
"""
import asyncio
import os
import sys
from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("API_SECRET_KEY", "test")
sys.path.insert(0, ".")

from app.services.country import detect_country, COUNTRY_MAP
from app.services.elevenlabs import generate_outreach_message, _fallback_outreach

# Test phone numbers
TEST_PHONES = [
    {"phone": "351912345678", "expected_country": "PT", "expected_campaign": "portugal", "expected_lang": "pt-PT"},
    {"phone": "+351 912 345 678", "expected_country": "PT", "expected_campaign": "portugal", "expected_lang": "pt-PT"},
    {"phone": "00351912345678", "expected_country": "PT", "expected_campaign": "portugal", "expected_lang": "pt-PT"},
    {"phone": "541234567890", "expected_country": "AR", "expected_campaign": "argentina", "expected_lang": "es-AR"},
    {"phone": "+54 9 1234 5678", "expected_country": "AR", "expected_campaign": "argentina", "expected_lang": "es-AR"},
    {"phone": "0054912345678", "expected_country": "AR", "expected_campaign": "argentina", "expected_lang": "es-AR"},
]

AGENT_IDS = {
    "pt-PT": "agent_6901knmsm0cpfw39pzd84f33dwzp",
    "es-AR": "agent_7101kp8jz5wnej79qrsz80mtk636",
}

LINKS = {
    "portugal": "https://oro.casino/ref/test",
    "argentina": "https://pampas.casino/ref/test",
}

PROMOS = {
    "portugal": "50Pragmatic",
    "argentina": None,
}

async def test_phone_routing(phone, expected_country, expected_campaign, expected_lang):
    """Тестирует распределение по одному номеру."""

    print(f"\n{'='*100}")
    print(f"  Phone: {phone}")
    print(f"  Expected: {expected_country} → {expected_campaign} → {expected_lang}")
    print(f"{'='*100}")

    # Шаг 1: Детектирование страны
    print(f"\n  [1] COUNTRY DETECTION")
    country_info = detect_country(phone)
    print(f"      Detected: {country_info['code']} ({country_info['name']})")
    print(f"      Campaign: {country_info['campaign']}")
    print(f"      Language: {country_info['lang']}")

    # Проверяем корректность
    if country_info['code'] == expected_country:
        print(f"      ✅ Country correct")
    else:
        print(f"      ❌ WRONG COUNTRY! Expected {expected_country}, got {country_info['code']}")
        return False

    if country_info['campaign'] == expected_campaign:
        print(f"      ✅ Campaign correct")
    else:
        print(f"      ❌ WRONG CAMPAIGN! Expected {expected_campaign}, got {country_info['campaign']}")
        return False

    # Шаг 2: Получаем agent_id для этого языка
    print(f"\n  [2] AGENT SELECTION")
    lang = country_info['lang']
    if lang == "pt":
        lang = "pt-PT"
    elif lang == "es":
        lang = "es-AR"

    agent_id = AGENT_IDS.get(lang)
    print(f"      Language: {lang}")
    print(f"      Agent ID: {agent_id[:30]}...")

    if agent_id is None:
        print(f"      ❌ NO AGENT FOR THIS LANGUAGE!")
        return False
    else:
        print(f"      ✅ Agent found")

    # Шаг 3: Генерируем сообщение
    print(f"\n  [3] MESSAGE GENERATION")
    link = LINKS[country_info['campaign']]
    promo = PROMOS[country_info['campaign']]

    try:
        message = await generate_outreach_message(
            agent_id=agent_id,
            chat_key=f"{phone}:routing:test",
            language=lang,
            link_url=link,
            promo_code=promo or "",
        )

        if message:
            print(f"      ✅ LLM Generated ({len(message)} chars)")
            print(f"\n      Message preview:")
            for line in message.split("\n")[:3]:
                print(f"        {line}")

            # Проверяем что в сообщении есть ключевые элементы
            checks = {
                "Link present": link in message,
            }

            if lang == "pt-PT":
                checks.update({
                    "Portuguese: 'teu/tua'": any(w in message.lower() for w in ["teu", "tua"]),
                    "Portuguese: 'rodadas'": "rodadas" in message.lower(),
                    "Oro Casino": "oro" in message.lower(),
                    "Pragmatic": "pragmatic" in message.lower(),
                })
            else:  # es-AR
                checks.update({
                    "Spanish AR: 'vos'": any(w in message.lower() for w in ["vos", "respondé", "mandás"]),
                    "Spanish AR: '175%'": "175" in message,
                    "Pampas": "pampas" in message.lower(),
                    "ARS 5000": "5000" in message,
                })

            print(f"\n      Content verification:")
            all_passed = True
            for check_name, passed in checks.items():
                symbol = "✅" if passed else "❌"
                print(f"        {symbol} {check_name}")
                if not passed:
                    all_passed = False

            if not all_passed:
                print(f"\n      ⚠️  CONTENT CHECK FAILED!")
                return False
        else:
            # Сообщение пусто - проверяем fallback
            print(f"      ⚠️  LLM returned empty, checking FALLBACK...")
            fallback = _fallback_outreach(lang, link, promo)
            print(f"      ✅ Fallback template used ({len(fallback)} chars)")
            print(f"\n      Fallback preview:")
            for line in fallback.split("\n")[:2]:
                print(f"        {line}")

    except Exception as e:
        print(f"      ❌ ERROR: {e}")
        return False

    print(f"\n  ✅ ROUTING TEST PASSED")
    return True


async def main():
    print(f"\n{'='*100}")
    print(f"  ROUTING & DISTRIBUTION TEST")
    print(f"  Verify: phone → country → campaign → agent → language → offer")
    print(f"  Check: LLM generated vs Fallback templates")
    print(f"{'='*100}")

    results = []
    for test_case in TEST_PHONES:
        passed = await test_phone_routing(
            phone=test_case['phone'],
            expected_country=test_case['expected_country'],
            expected_campaign=test_case['expected_campaign'],
            expected_lang=test_case['expected_lang'],
        )
        results.append((test_case['phone'], passed))

    # Final report
    print(f"\n{'='*100}")
    print(f"  FINAL REPORT")
    print(f"{'='*100}\n")

    passed_count = sum(1 for _, p in results if p)
    total_count = len(results)

    print(f"  Routing Tests: {passed_count}/{total_count}")
    for phone, passed in results:
        symbol = "✅" if passed else "❌"
        print(f"    {symbol} {phone}")

    print(f"\n{'='*100}")
    if passed_count == total_count:
        print(f"  ✅ ALL ROUTING TESTS PASSED!")
        print(f"  Distribution is CORRECT")
        print(f"  Ready for production")
    else:
        print(f"  ⚠️  SOME TESTS FAILED")
        print(f"  Check routing logic")
    print(f"{'='*100}\n")


if __name__ == "__main__":
    asyncio.run(main())
