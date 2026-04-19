"""Create 3 test staff on production to battle-test RBAC.  Delete after use."""
import asyncio, asyncpg

async def run():
    url = "postgresql://postgres.vllqryousoshbfakixup:Burptech101023@aws-1-ap-northeast-2.pooler.supabase.com:6543/postgres"
    conn = await asyncpg.connect(url, statement_cache_size=0)

    branch_id = "82ac3013-14cb-4e1a-acd8-3d5f921dafb8"
    owner_id = "a07da9d2-1235-4af5-bcd3-9ba56b6edc47"

    test_staff = [
        ("90ac6fdc-be2c-4fc3-82ee-2a384243374f", "manager", "boracay241193@gmail.com"),
        ("92d8d57b-c882-4484-a223-a7dd41e7652e", "cashier", "dffboracaytwo2024@gmail.com"),
        ("9cc0e5ac-e19b-440a-a09f-9456ebf3e433", "waiter", "thoratmj12@gmail.com"),
    ]

    for user_id, role, email in test_staff:
        role_row = await conn.fetchrow(
            "SELECT id FROM roles WHERE branch_id = $1 AND lower(name) = lower($2) LIMIT 1",
            branch_id, role
        )
        role_id = role_row["id"] if role_row else None

        await conn.execute(
            "INSERT INTO branch_users (user_id, branch_id, owner_id, role, role_id, is_active) "
            "VALUES ($1, $2::uuid, $3, $4, $5, true) ON CONFLICT DO NOTHING",
            user_id, branch_id, owner_id, role, role_id
        )
        print(f"Created: {email} as {role} (role_id={role_id})")

    bus = await conn.fetch("SELECT user_id, branch_id, role, role_id FROM branch_users")
    print(f"\nTotal branch_users: {len(bus)}")
    for bu in bus:
        print(" ", dict(bu))

    # Verify cashier permissions resolve from DB
    cashier_perms = await conn.fetch(
        "SELECT p.key, rp.allowed, rp.meta "
        "FROM role_permissions rp "
        "JOIN permissions p ON p.id = rp.permission_id "
        "WHERE rp.role_id = (SELECT role_id FROM branch_users WHERE role = 'cashier' LIMIT 1)"
    )
    print(f"\nCashier permissions from DB ({len(cashier_perms)}):")
    for p in cashier_perms:
        meta = p["meta"] if p["meta"] else {}
        print(f"  {p['key']}: allowed={p['allowed']} {meta}")

    # Verify waiter has NO payment.refund
    waiter_perms = await conn.fetch(
        "SELECT p.key FROM role_permissions rp "
        "JOIN permissions p ON p.id = rp.permission_id "
        "WHERE rp.role_id = (SELECT role_id FROM branch_users WHERE role = 'waiter' LIMIT 1) "
        "AND rp.allowed = true"
    )
    waiter_keys = {p["key"] for p in waiter_perms}
    print(f"\nWaiter permissions ({len(waiter_keys)}): {sorted(waiter_keys)}")
    assert "payment.refund" not in waiter_keys, "FAIL: waiter should NOT have payment.refund"
    assert "payment.create" not in waiter_keys, "FAIL: waiter should NOT have payment.create"
    assert "order.create" in waiter_keys, "FAIL: waiter should have order.create"
    print("PASS: waiter permission boundaries correct")

    await conn.close()

asyncio.run(run())
