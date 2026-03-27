"""
One-time fix: Update google_connections rows where restaurant_id = user_id
to use the actual restaurant ID from the restaurants table.
"""
import asyncio, asyncpg, os

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], statement_cache_size=0)

    # Find all google_connections where restaurant_id == user_id (the bug)
    rows = await conn.fetch("""
        SELECT gc.id, gc.user_id, gc.restaurant_id, r.id AS real_restaurant_id, r.name
        FROM google_connections gc
        JOIN restaurants r ON r.owner_id::text = gc.user_id::text
        WHERE gc.restaurant_id::text = gc.user_id::text
    """)

    if not rows:
        print("No broken rows found. All good!")
        await conn.close()
        return

    print(f"Found {len(rows)} rows to fix:\n")
    for r in rows:
        print(f"  connection={r['id']}")
        print(f"    user_id={r['user_id']}")
        print(f"    restaurant_id (WRONG): {r['restaurant_id']}")
        print(f"    restaurant_id (CORRECT): {r['real_restaurant_id']}  ({r['name']})")

    print("\nFixing...")
    for r in rows:
        await conn.execute(
            "UPDATE google_connections SET restaurant_id = $1::text, updated_at = now() WHERE id = $2",
            str(r['real_restaurant_id']),
            r['id'],
        )
        print(f"  Fixed connection {r['id']}: {r['restaurant_id']} -> {r['real_restaurant_id']}")

    print("\nDone! Verifying...")
    after = await conn.fetch("SELECT id, user_id, restaurant_id, account_id, location_id FROM google_connections")
    for r in after:
        print(f"  id={r['id']} user={r['user_id']} restaurant={r['restaurant_id']} account={r['account_id']} location={r['location_id']}")

    await conn.close()

asyncio.run(main())
