import asyncio, asyncpg, pathlib
SQL = pathlib.Path("migrations/061_razorpay_role_grants.sql").read_bytes().decode("utf-8", errors="ignore")
DSN = "postgresql://postgres.vllqryousoshbfakixup:Burptech101023@aws-1-ap-northeast-2.pooler.supabase.com:6543/postgres"

async def main():
    conn = await asyncpg.connect(DSN, statement_cache_size=0)
    try:
        await conn.execute(SQL)
        rows = await conn.fetch(
            """
            SELECT r.name AS role, COUNT(*) AS razorpay_perms
            FROM role_permissions rp
            JOIN roles r       ON r.id = rp.role_id
            JOIN permissions p ON p.id = rp.permission_id
            WHERE p.key LIKE 'razorpay.%' AND rp.allowed = true
            GROUP BY r.name
            ORDER BY r.name
            """
        )
        print("razorpay grants by role (sample across branches):")
        for row in rows:
            print(f"  {row['role']:<10} {row['razorpay_perms']}")
        # spot-check that the failing perm specifically lands on owner
        owner_has = await conn.fetchval(
            """
            SELECT COUNT(*) FROM role_permissions rp
            JOIN roles r       ON r.id = rp.role_id
            JOIN permissions p ON p.id = rp.permission_id
            WHERE p.key = 'razorpay.orders.read'
              AND lower(r.name) = 'owner'
              AND rp.allowed = true
            """
        )
        print(f"owner→razorpay.orders.read rows: {owner_has}")
    finally:
        await conn.close()

asyncio.run(main())
