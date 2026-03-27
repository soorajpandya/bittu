import asyncio, asyncpg, os

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], statement_cache_size=0)

    uid = "cc0d821d-cc05-4b1e-8064-541e781d406f"
    rid_sent = "6b2cd124-a04a-4056-9054-8b27b74ffbc0"

    print("=== User context ===")
    # Check if this user owns a restaurant
    r = await conn.fetchrow("SELECT id, owner_id, name FROM restaurants WHERE owner_id = $1", uid)
    if r:
        print(f"Owns restaurant: id={r['id']}, owner_id={r['owner_id']}, name={r['name']}")
    else:
        print(f"No restaurant owned by uid={uid}")

    # Check branch_users
    r2 = await conn.fetchrow("SELECT user_id, branch_id, owner_id, role FROM branch_users WHERE user_id = $1", uid)
    if r2:
        print(f"Branch user: owner_id={r2['owner_id']}, role={r2['role']}, branch_id={r2['branch_id']}")
    else:
        print("Not a branch user")

    print(f"\n=== Restaurant sent by frontend: {rid_sent} ===")
    r3 = await conn.fetchrow("SELECT id, owner_id, name FROM restaurants WHERE id = $1", rid_sent)
    if r3:
        print(f"Restaurant found: id={r3['id']}, owner_id={r3['owner_id']}, name={r3['name']}")
    else:
        print("No restaurant with that ID")

    r4 = await conn.fetchrow("SELECT id, owner_id, name FROM restaurants WHERE owner_id = $1", rid_sent)
    if r4:
        print(f"Owned by that ID: id={r4['id']}, owner_id={r4['owner_id']}, name={r4['name']}")

    print("\n=== Google connections ===")
    rows = await conn.fetch("SELECT user_id, restaurant_id, is_active, account_id, location_id FROM google_connections")
    for r in rows:
        print(f"  user_id={r['user_id']}, restaurant_id={r['restaurant_id']}, active={r['is_active']}, account={r['account_id']}, location={r['location_id']}")

    await conn.close()

asyncio.run(main())
