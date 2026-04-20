"""Run migration 023 on the production database."""
import asyncio, os, asyncpg

async def run():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], statement_cache_size=0)
    sql = open("migrations/023_immutability_integrity.sql").read()
    await conn.execute(sql)
    print("Migration 023 applied successfully")

    # Verify triggers
    triggers = await conn.fetch(
        "SELECT tgname FROM pg_trigger WHERE tgname IN ('trg_immutable_journal_entries', 'trg_immutable_journal_lines') ORDER BY tgname"
    )
    for t in triggers:
        print(f"  Trigger: {t['tgname']}")

    # Verify function
    funcs = await conn.fetch(
        "SELECT proname FROM pg_proc WHERE proname = 'fn_check_accounting_integrity'"
    )
    for f in funcs:
        print(f"  Function: {f['proname']}")

    await conn.close()

asyncio.run(run())
