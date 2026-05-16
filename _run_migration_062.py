import asyncio, asyncpg, pathlib
SQL = pathlib.Path("migrations/062_backfill_role_permissions.sql").read_bytes().decode("utf-8", errors="ignore")
DSN = "postgresql://postgres.vllqryousoshbfakixup:Burptech101023@aws-1-ap-northeast-2.pooler.supabase.com:6543/postgres"

CHECK_KEYS = (
    "menu.read",
    "waitlist.read",
    "order.create",
    "order.read",
    "payment.create",
    "razorpay.orders.read",
)


async def main():
    conn = await asyncpg.connect(DSN, statement_cache_size=0)
    try:
        # Before snapshot
        before = await conn.fetch(
            """
            SELECT lower(r.name) AS role, p.key,
                   COUNT(*) FILTER (WHERE rp.allowed) AS allowed_rows,
                   COUNT(*)                            AS role_rows_total
            FROM roles r
            CROSS JOIN permissions p
            LEFT JOIN role_permissions rp
                   ON rp.role_id = r.id AND rp.permission_id = p.id
            WHERE p.key = ANY($1::text[])
            GROUP BY lower(r.name), p.key
            ORDER BY lower(r.name), p.key
            """,
            list(CHECK_KEYS),
        )
        print("--- BEFORE (allowed grants / total role rows of that name) ---")
        for r in before:
            print(f"  {r['role']:<10} {r['key']:<24} {r['allowed_rows']}/{r['role_rows_total']}")

        await conn.execute(SQL)

        after = await conn.fetch(
            """
            SELECT lower(r.name) AS role, p.key,
                   COUNT(*) FILTER (WHERE rp.allowed) AS allowed_rows,
                   COUNT(*)                            AS role_rows_total
            FROM roles r
            CROSS JOIN permissions p
            LEFT JOIN role_permissions rp
                   ON rp.role_id = r.id AND rp.permission_id = p.id
            WHERE p.key = ANY($1::text[])
            GROUP BY lower(r.name), p.key
            ORDER BY lower(r.name), p.key
            """,
            list(CHECK_KEYS),
        )
        print("\n--- AFTER ---")
        for r in after:
            print(f"  {r['role']:<10} {r['key']:<24} {r['allowed_rows']}/{r['role_rows_total']}")

        # totals
        totals = await conn.fetch(
            """
            SELECT lower(r.name) AS role, COUNT(*) AS perm_count
            FROM role_permissions rp
            JOIN roles r ON r.id = rp.role_id
            WHERE rp.allowed = true
            GROUP BY lower(r.name)
            ORDER BY lower(r.name)
            """
        )
        print("\n--- total allowed perms per role row name (sum across branches) ---")
        for r in totals:
            print(f"  {r['role']:<10} {r['perm_count']}")
    finally:
        await conn.close()


asyncio.run(main())
