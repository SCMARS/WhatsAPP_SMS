import asyncio
from sqlalchemy import select
from app.db.session import async_session
from app.db.models import WhatsAppInstance

async def main():
    async with async_session() as db:
        res = await db.execute(select(WhatsAppInstance))
        instances = res.scalars().all()
        print("INSTANCES IN DB:")
        for i in instances:
            print(f"ID={i.instance_id} Active={i.is_active} Banned={i.is_banned}")

if __name__ == "__main__":
    asyncio.run(main())
