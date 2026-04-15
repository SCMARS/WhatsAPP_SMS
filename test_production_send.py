"""
test_production_send.py — Полный production тест:
1. Генерирует сообщения от правильных агентов
2. Разделяет на 3 части
3. Отправляет через Green API на реальный номер
4. Проверяет что отправилось

ВАЖНО: это РЕАЛЬНАЯ отправка! Проверьте PHONE перед запуском.
"""
import asyncio
import os
import sys
from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("API_SECRET_KEY", "test")
os.environ.setdefault("APP_HOST", "0.0.0.0")
os.environ.setdefault("APP_PORT", "8000")

sys.path.insert(0, ".")

from app.db.session import AsyncSessionLocal
from app.db.models import Campaign, Conversation, Instance
from app.services.elevenlabs import generate_outreach_message
from app.api.routes import _split_outreach_into_three_random_parts
from app.services.sender import send_initial_message
from sqlalchemy import select
import uuid

# КОНФИГ
AGENT_ID_PT = "agent_6901knmsm0cpfw39pzd84f33dwzp"  # Oro/Camila
AGENT_ID_AR = "agent_7101kp8jz5wnej79qrsz80mtk636"  # Pampas/Olivia

# ⚠️  ИЗМЕНИТЕ НА РЕАЛЬНЫЙ НОМЕР ДЛЯ ТЕСТИРОВАНИЯ
TEST_CONFIGS = [
    {
        "name": "Oro Casino (PT)",
        "phone": "351912345678",  # Portugal number (adjust!)
        "lang": "pt-PT",
        "agent_id": AGENT_ID_PT,
        "link": "https://oro.casino/ref/test-production-pt",
        "promo": "50Pragmatic",
        "campaign_id": "portugal",
    },
    {
        "name": "Pampas Casino (AR)",
        "phone": "541234567890",  # Argentina number (adjust!)
        "lang": "es-AR",
        "agent_id": AGENT_ID_AR,
        "link": "https://pampas.casino/ref/test-production-ar",
        "promo": None,
        "campaign_id": "argentina",
    },
]

SEP  = "─" * 80
SEP2 = "═" * 80

async def test_send(config):
    """Генерирует, разделяет и отправляет на реальный номер."""

    print(f"\n{SEP2}")
    print(f"  PRODUCTION SEND: {config['name']}")
    print(f"  Phone: {config['phone']}")
    print(f"  Language: {config['lang']}")
    print(SEP2)

    async with AsyncSessionLocal() as db:
        # 1. Найти кампанию
        print(f"\n  [1] Поиск кампании '{config['campaign_id']}'...")
        res = await db.execute(
            select(Campaign).where(Campaign.external_id == config['campaign_id'])
        )
        campaign = res.scalar_one_or_none()

        if not campaign:
            print(f"    ❌ Кампания не найдена!")
            print(f"    💡 Попробуйте создать кампанию через API или БД")
            return False

        print(f"    ✓ Найдена: id={campaign.id}, agent={campaign.agent_id}")

        # Убедимся что используется правильный agent_id
        if campaign.agent_id != config['agent_id']:
            print(f"\n    ⚠️  ВНИМАНИЕ: В campaign другой agent_id!")
            print(f"       В campaign: {campaign.agent_id}")
            print(f"       Нужен: {config['agent_id']}")
            print(f"       Продолжаю с agent_id из campaign...")

        # 2. Найти или создать conversation
        print(f"\n  [2] Управление conversation...")
        res = await db.execute(
            select(Conversation).where(
                Conversation.campaign_id == campaign.id,
                Conversation.phone == config['phone'],
            )
        )
        conv = res.scalar_one_or_none()

        if conv is None:
            print(f"    Создаю новый conversation...")
            conv = Conversation(
                campaign_id=campaign.id,
                lead_id=f"prod-test-{uuid.uuid4().hex[:8]}",
                phone=config['phone'],
                lead_name="TestLead",
                status="active",
            )
            db.add(conv)
            await db.commit()
            await db.refresh(conv)
            print(f"    ✓ Создан: {conv.id}")
        else:
            print(f"    ✓ Найден: {conv.id}")

        # 3. Генерируем сообщение
        print(f"\n  [3] Генерирую сообщение от агента...")
        try:
            raw = await generate_outreach_message(
                agent_id=campaign.agent_id,  # Используем agent_id из campaign
                chat_key=f"{config['phone']}:prod:test",
                language=config['lang'],
                link_url=config['link'],
                promo_code=config['promo'] or "",
            )

            if not raw:
                print(f"    ❌ Агент вернул пусто!")
                return False

            print(f"    ✓ Получено сообщение ({len(raw)} символов)")
            print(f"\n    Текст:")
            for line in raw.split("\n"):
                print(f"      {line}")

        except Exception as e:
            print(f"    ❌ ОШИБКА генерации: {e}")
            import traceback
            traceback.print_exc()
            return False

        # 4. Разделяем на 3 части
        print(f"\n  [4] Разделяю на 3 части...")
        try:
            parts = _split_outreach_into_three_random_parts(
                raw,
                link_url=config['link'],
                promo_code=config['promo']
            )
            print(f"    ✓ Разделено на {len(parts)} частей")

            for i, part in enumerate(parts, 1):
                print(f"\n      Часть {i}:")
                for line in part.split("\n"):
                    print(f"        {line}")

            # Проверяем обязательные поля в последней части
            last_part = parts[-1] if parts else ""
            if config['link'] not in last_part:
                print(f"    ⚠️  ВНИМАНИЕ: Ссылка отсутствует в последней части!")
            else:
                print(f"    ✓ Ссылка в последней части")

            if config['promo'] and config['promo'] not in last_part:
                print(f"    ⚠️  ВНИМАНИЕ: Промо отсутствует в последней части!")
            elif config['promo']:
                print(f"    ✓ Промо в последней части")

        except Exception as e:
            print(f"    ❌ ОШИБКА разделения: {e}")
            import traceback
            traceback.print_exc()
            return False

        # 5. Отправляем через Green API
        print(f"\n  [5] Отправляю через Green API...")
        try:
            msg = await send_initial_message(
                db=db,
                conversation=conv,
                initial_text=parts,
                batch_index=0
            )

            if msg:
                print(f"    ✅ ОТПРАВЛЕНО!")
                print(f"       provider_id: {msg.provider_message_id}")
                print(f"       status: {msg.status}")
                print(f"       sent_at: {msg.sent_at}")
                return True
            else:
                print(f"    ❌ Отправка вернула None")
                print(f"       Проверьте instance и Green API credentials")
                return False

        except Exception as e:
            print(f"    ❌ ОШИБКА отправки: {e}")
            import traceback
            traceback.print_exc()
            return False


async def main():
    print(f"\n{'='*80}")
    print(f"  PRODUCTION SEND TEST")
    print(f"  Oro Casino (PT) + Pampas (AR)")
    print(f"  ⚠️  РЕАЛЬНАЯ ОТПРАВКА! Проверьте номера перед запуском")
    print(f"{'='*80}")

    results = []
    for config in TEST_CONFIGS:
        success = await test_send(config)
        results.append((config['name'], success))

    print(f"\n{SEP2}")
    print(f"  ИТОГОВЫЙ ОТЧЕТ")
    print(SEP2)

    for name, success in results:
        status = "✅ OK" if success else "❌ FAILED"
        print(f"  {status} {name}")

    all_ok = all(s for _, s in results)
    print(f"\n{'='*80}")
    if all_ok:
        print(f"  ✅ ВСЕ ТЕСТЫ ПРОЙДЕНЫ!")
        print(f"  🚀 Готово к production")
    else:
        print(f"  ⚠️  НЕКОТОРЫЕ ТЕСТЫ НЕ ПРОШЛИ")
        print(f"  Проверьте логи выше")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    asyncio.run(main())
