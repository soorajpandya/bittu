import asyncio, asyncpg

async def run():
    conn = await asyncpg.connect(
        'postgresql://postgres.vllqryousoshbfakixup:Burptech101023@aws-1-ap-northeast-2.pooler.supabase.com:6543/postgres',
        statement_cache_size=0,
    )
    with open('migrations/016_remaining_permissions.sql') as f:
        sql = f.read()
    await conn.execute(sql)
    count = await conn.fetchval('SELECT count(*) FROM permissions')
    rp_count = await conn.fetchval('SELECT count(*) FROM role_permissions')
    print(f'Permissions: {count}, Role-Permission mappings: {rp_count}')
    await conn.close()

asyncio.run(run())
