"""List all public tables with row counts."""
import asyncio
from app.core.database import init_db_pool, close_db_pool, get_connection


async def main():
    await init_db_pool()
    async with get_connection() as c:
        rows = await c.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        )
        for r in rows:
            t = r["tablename"]
            try:
                n = await c.fetchval(f'SELECT COUNT(*) FROM "{t}"')
            except Exception as e:
                n = f"ERR:{e}"
            print(f"{t}\t{n}")
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
