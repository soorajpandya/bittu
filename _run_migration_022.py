"""Run migration 022 on the production database."""
import asyncio, os, asyncpg

async def run():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], statement_cache_size=0)
    sql = open("migrations/022_bank_recon_period_lock_reports.sql").read()
    await conn.execute(sql)
    print("Migration 022 applied successfully")
    tables = await conn.fetch("SELECT tablename FROM pg_tables WHERE tablename IN ('bank_statements','bank_reconciliation') ORDER BY tablename")
    for t in tables:
        print(f"  Table: {t['tablename']}")
    triggers = await conn.fetch("SELECT tgname FROM pg_trigger WHERE tgname = 'trg_enforce_period_lock'")
    for t in triggers:
        print(f"  Trigger: {t['tgname']}")
    perms = await conn.fetch("SELECT key FROM permissions WHERE key IN ('bank_recon.read','bank_recon.write','reports.read') ORDER BY key")
    for p in perms:
        print(f"  Permission: {p['key']}")
    await conn.close()

asyncio.run(run())
