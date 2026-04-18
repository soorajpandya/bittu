import asyncio, asyncpg, os

async def run():
    url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not url:
        print("ERROR: No DATABASE_URL or SUPABASE_DB_URL found")
        return
    conn = await asyncpg.connect(url, statement_cache_size=0)

    # Drop existing policies if they exist, then re-run migration
    cleanup = """
    DROP POLICY IF EXISTS waitlist_entries_tenant ON waitlist_entries;
    DROP POLICY IF EXISTS waitlist_settings_tenant ON waitlist_settings;
    DROP POLICY IF EXISTS waitlist_history_tenant ON waitlist_history;
    DROP POLICY IF EXISTS waitlist_entries_service ON waitlist_entries;
    DROP POLICY IF EXISTS waitlist_settings_service ON waitlist_settings;
    DROP POLICY IF EXISTS waitlist_history_service ON waitlist_history;
    """
    await conn.execute(cleanup)
    print("Dropped existing policies")

    sql = open("migrations/009_waitlist.sql").read()
    await conn.execute(sql)
    print("Migration 009_waitlist.sql applied successfully")
    await conn.close()

asyncio.run(run())
