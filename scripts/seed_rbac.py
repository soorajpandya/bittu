"""Seed default RBAC roles and permissions for all branches."""
import asyncio

from app.core.database import init_db_pool, close_db_pool, get_connection


PERMISSIONS = [
    "order.create",
    "order.edit",
    "order.cancel",
    "order.read",
    "orders.create",
    "orders.read",
    "orders.update",
    "billing.generate",
    "billing.discount",
    "payment.create",
    "payment.refund",
    "payments.create",
    "payments.refund",
    "table.read",
    "table.start",
    "table.close",
    "table.manage",
    "tables.manage",
    "inventory.read",
    "inventory.update",
    "inventory.manage",
    "voice.use",
    "kitchen.read",
]

ROLES = ["owner", "manager", "cashier", "waiter", "kitchen", "staff"]


async def seed() -> None:
    await init_db_pool()
    try:
        async with get_connection() as conn:
            for key in PERMISSIONS:
                await conn.execute("INSERT INTO permissions (key) VALUES ($1) ON CONFLICT (key) DO NOTHING", key)

            branches = await conn.fetch("SELECT id FROM sub_branches")
            for b in branches:
                for role_name in ROLES:
                    await conn.execute(
                        """
                        INSERT INTO roles (name, branch_id, is_default)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (branch_id, name) DO NOTHING
                        """,
                        role_name,
                        b["id"],
                        True,
                    )

        print("RBAC seed complete")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(seed())
