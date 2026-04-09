import asyncio, asyncpg, os
from dotenv import load_dotenv
load_dotenv()

async def t():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'], statement_cache_size=0)
    for tbl in ['items', 'categories', 'customers', 'coupons', 'restaurant_tables', 'acc_contacts', 'acc_taxes']:
        r = await conn.fetchrow(f'SELECT * FROM {tbl} LIMIT 1')
        if r:
            d = dict(r)
            keys = list(d.keys())[:8]
            has_id = 'id' in d
            print(f'{tbl}: has_id={has_id}, first_cols={keys}')
        else:
            cols = await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name=$1 ORDER BY ordinal_position LIMIT 8",
                tbl
            )
            print(f'{tbl}: EMPTY, cols={[c["column_name"] for c in cols]}')
    await conn.close()

asyncio.run(t())
