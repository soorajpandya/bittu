import asyncio, asyncpg, os

async def run():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'], statement_cache_size=0)
    with open('migrations/015_erp_subscription_billing_cash_permissions.sql') as f:
        sql = f.read()
    await conn.execute(sql)
    count = await conn.fetchval("SELECT count(*) FROM permissions WHERE key LIKE 'erp.%' OR key LIKE 'subscription.%' OR key = 'billing.read' OR key LIKE 'cash_transaction.%'")
    print(f'New permission keys in DB: {count}')
    rp = await conn.fetchval("SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id WHERE p.key LIKE 'erp.%' OR p.key LIKE 'subscription.%' OR p.key = 'billing.read' OR p.key LIKE 'cash_transaction.%'")
    print(f'New role-permission mappings: {rp}')
    total_perms = await conn.fetchval("SELECT count(*) FROM permissions")
    total_rps = await conn.fetchval("SELECT count(*) FROM role_permissions")
    print(f'Total permissions: {total_perms}, Total role-permission mappings: {total_rps}')
    await conn.close()

asyncio.run(run())
