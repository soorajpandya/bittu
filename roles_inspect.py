import asyncio
import asyncpg

async def main():
    DSN = 'postgresql://postgres.vllqryousoshbfakixup:Burptech101023@aws-1-ap-northeast-2.pooler.supabase.com:6543/postgres'
    conn = await asyncpg.connect(DSN, statement_cache_size=0)
    
    print("--- Roles Table (lower(name)) ---")
    roles = await conn.fetch('SELECT lower(name) AS name, COUNT(*) AS n FROM roles GROUP BY lower(name) ORDER BY name')
    if not roles:
        print("No roles found.")
    for r in roles:
        print(f"{r['name']}: {r['n']}")
        
    print("\n--- Branch Users Table Check ---")
    # First check if the table exists/has data
    count = await conn.fetchval('SELECT COUNT(*) FROM branch_users')
    print(f"Total branch_users: {count}")
    
    if count > 0:
        print("\n--- Branch Users Grouped by Role ---")
        branch_users = await conn.fetch('SELECT role, COUNT(*) AS n FROM branch_users GROUP BY role ORDER BY role')
        for r in branch_users:
            print(f"{r['role']}: {r['n']}")
    else:
        print("branch_users table is empty.")
        
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
