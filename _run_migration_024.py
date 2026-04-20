#!/usr/bin/env python3
"""Run migration 024 — Financial Operating System."""
import asyncio, asyncpg, os, pathlib

async def main():
    url = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(url, statement_cache_size=0)
    sql = pathlib.Path("/home/ubuntu/bittu/migrations/024_financial_operating_system.sql").read_text()
    try:
        await conn.execute(sql)
        print("Migration 024 applied OK")
    except Exception as e:
        print(f"Migration 024 error: {e}")
    finally:
        await conn.close()

asyncio.run(main())
