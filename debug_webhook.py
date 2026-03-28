import asyncio, logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")

from app.db.session import AsyncSessionLocal
from app.webhook.handler import handle_incoming

payload = {
    "typeWebhook": "incomingMessageReceived",
    "instanceData": {"idInstance": "7107566873"},
    "timestamp": 1711630999,
    "idMessage": "DEBUG_TEST_XYZ",
    "senderData": {"chatId": "380671202709@c.us", "senderName": "Test"},
    "messageData": {
        "typeMessage": "textMessage",
        "textMessageData": {"textMessage": "Hi, what bonuses do I have?"}
    }
}

async def main():
    async with AsyncSessionLocal() as db:
        try:
            await handle_incoming(db, payload, "7107566873")
            print("HANDLER DONE OK")
        except Exception as e:
            print(f"HANDLER EXCEPTION: {e}")
            import traceback; traceback.print_exc()

asyncio.run(main())
