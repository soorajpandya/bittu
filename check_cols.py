import asyncio, asyncpg, os

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], statement_cache_size=0)

    rid = "6b2cd124-a04a-4056-9054-8b27b74ffbc0"

    # Check restaurant owner
    r = await conn.fetchrow("SELECT id, owner_id, name FROM restaurants WHERE id = $1", rid)
    if r:
        print(f"Restaurant: id={r['id']}, owner_id={r['owner_id']}, name={r['name']}")
    else:
        print(f"No restaurant with id={rid}")

    # Check if this ID is an owner
    r2 = await conn.fetchrow("SELECT id, owner_id, name FROM restaurants WHERE owner_id = $1", rid)
    if r2:
        print(f"Owns restaurant: id={r2['id']}, owner_id={r2['owner_id']}, name={r2['name']}")
    else:
        print(f"No restaurant owned by {rid}")

    # Check google_connections
    r3 = await conn.fetchrow("SELECT user_id, restaurant_id, is_active FROM google_connections WHERE restaurant_id = $1", rid)
    if r3:
        print(f"Google conn: user_id={r3['user_id']}, restaurant_id={r3['restaurant_id']}, active={r3['is_active']}")
    else:
        r4 = await conn.fetchrow("SELECT user_id, restaurant_id, is_active FROM google_connections WHERE user_id = $1", rid)
        if r4:
            print(f"Google conn (by user): user_id={r4['user_id']}, restaurant_id={r4['restaurant_id']}, active={r4['is_active']}")
        else:
            print("No google_connections found")

    await conn.close()

asyncio.run(main())
