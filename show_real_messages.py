"""
show_real_messages.py — Показать реальные сообщения которые будут отправляться
"""
import asyncio
import os
import sys
from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("API_SECRET_KEY", "test")
sys.path.insert(0, ".")

from app.services.elevenlabs import generate_outreach_message

CONFIGS = [
    ("🇵🇹 PORTUGAL (Oro/Camila)", "agent_6901knmsm0cpfw39pzd84f33dwzp", "pt-PT", "https://oro.casino/ref/live_351", "50Pragmatic"),
    ("🇦🇷 ARGENTINA (Pampas/Olivia)", "agent_7101kp8jz5wnej79qrsz80mtk636", "es-AR", "https://pampas.casino/ref/live_54", None),
]

async def main():
    print("\n" + "="*100)
    print("  📱 РЕАЛЬНЫЕ СООБЩЕНИЯ КОТОРЫЕ ОТПРАВЛЯЮТСЯ В WHATSAPP")
    print("="*100)

    for name, agent_id, lang, link, promo in CONFIGS:
        print(f"\n\n{name}")
        print("="*100)

        for attempt in range(1, 4):
            msg = await generate_outreach_message(
                agent_id=agent_id,
                chat_key=f"{lang}:show:{attempt}",
                language=lang,
                link_url=link,
                promo_code=promo or "",
            )

            if msg:
                print(f"\n📨 Сообщение #{attempt}:")
                print("┌" + "─"*98 + "┐")

                # Format as WhatsApp message
                for line in msg.split("\n"):
                    # Wrap long lines
                    if len(line) > 90:
                        words = line.split(" ")
                        current_line = ""
                        for word in words:
                            if len(current_line) + len(word) + 1 <= 90:
                                current_line += word + " "
                            else:
                                if current_line:
                                    print("│ " + current_line.strip())
                                current_line = word + " "
                        if current_line:
                            print("│ " + current_line.strip())
                    else:
                        print("│ " + line)

                print("└" + "─"*98 + "┘")
            else:
                print(f"\n❌ Сообщение #{attempt}: Ошибка генерации")

    print("\n" + "="*100)
    print("  ✅ ГОТОВО")
    print("="*100 + "\n")

if __name__ == "__main__":
    asyncio.run(main())
