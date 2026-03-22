import asyncio
from app.core.config import get_settings
import asyncpg

async def main():
    s = get_settings()
    conn = await asyncpg.connect(s.DATABASE_URL)
    user_id = 'b4ae5983-8497-4061-8324-b68bff8f78da'
    print('>>> checking restaurant')
    r = await conn.fetch('SELECT id, owner_id FROM public.restaurants WHERE owner_id=$1', user_id)
    print(r)
    print('>>> checking sub_branches')
    b = await conn.fetch('SELECT id, restaurant_id, owner_id, is_main_branch FROM public.sub_branches WHERE owner_id=$1', user_id)
    print(b)
    await conn.close()

asyncio.run(main())
