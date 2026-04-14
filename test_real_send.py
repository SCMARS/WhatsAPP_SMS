"""
test_real_send.py — прямая отправка на реальный номер, минуя country check.
Генерирует сообщение как AR (es-AR) и шлёт через Green API.
"""
import asyncio, os, sys
from dotenv import load_dotenv
load_dotenv()
os.environ.setdefault("API_SECRET_KEY", "test")
os.environ.setdefault("APP_HOST", "0.0.0.0")
os.environ.setdefault("APP_PORT", "8000")
sys.path.insert(0, ".")

from app.db.session import AsyncSessionLocal
from app.services.elevenlabs import generate_outreach_message
from app.api.routes import _split_outreach_into_three_random_parts
from app.services.sender import send_initial_message
from app.db.models import Campaign, Conversation
from sqlalchemy import select

PHONE    = "380671202709"
LANG     = "es-AR"
LINK     = "https://pampas.casino/ref/test380"
PROMO    = None
AGENT_ID = os.environ["AGENT_ID"]

async def main():
    async with AsyncSessionLocal() as db:
        # Берём кампанию argentina
        res = await db.execute(select(Campaign).where(Campaign.external_id == "argentina"))
        campaign = res.scalar_one_or_none()
        if not campaign:
            print("Кампания 'argentina' не найдена")
            return

        # Берём или создаём conversation
        res = await db.execute(
            select(Conversation).where(
                Conversation.campaign_id == campaign.id,
                Conversation.phone == PHONE,
            )
        )
        conv = res.scalar_one_or_none()
        if conv is None:
            conv = Conversation(
                campaign_id=campaign.id,
                lead_id="realtest-ar-380",
                phone=PHONE,
                lead_name="Carlos",
                status="active",
            )
            db.add(conv)
            await db.commit()
            await db.refresh(conv)
            print(f"Создан conversation id={conv.id}")
        else:
            print(f"Найден conversation id={conv.id}")

        # Генерируем
        print(f"\nГенерирую сообщение [{LANG}]...")
        raw = await generate_outreach_message(
            agent_id=AGENT_ID,
            chat_key=f"{PHONE}:realtest",
            language=LANG,
            link_url=LINK,
            promo_code=PROMO,
        )
        print(f"\n[RAW от ElevenLabs]:\n  {raw}")

        parts = _split_outreach_into_three_random_parts(raw)
        print(f"\n[Сплит на {len(parts)} части]:")
        for i, p in enumerate(parts, 1):
            print(f"  Часть {i}: {p}")

        # Отправляем
        print(f"\nОтправляю на {PHONE}...")
        msg = await send_initial_message(db=db, conversation=conv, initial_text=parts, batch_index=0)
        if msg:
            print(f"\n✅ ОТПРАВЛЕНО! provider_id={msg.provider_message_id} status={msg.status}")
        else:
            print("\n❌ Отправка не удалась (нет инстанса или ошибка)")

asyncio.run(main())
