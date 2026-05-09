"""Run the wallet snapshot CTE directly with the live restaurant id."""
import asyncio
from datetime import datetime, timezone
from app.core.database import init_db_pool, get_connection, close_db_pool
from app.services.merchant_wallet_service import CASH_METHODS

RID = "751c6d1d-1559-45f2-a24b-7ecd16678113"

SQL = """
WITH cash AS (
  SELECT
    COALESCE(SUM(amount) FILTER (WHERE status='completed'),0)::numeric(14,2) AS collected,
    COALESCE(SUM(amount) FILTER (WHERE status='refunded'), 0)::numeric(14,2) AS refunded,
    COUNT(*)              FILTER (WHERE status='completed')                  AS tx_count
  FROM payments
  WHERE restaurant_id = $1::uuid
    AND method = ANY($3::text[])
    AND ($2::timestamptz IS NULL OR created_at <= $2::timestamptz)
)
SELECT * FROM cash;
"""


async def main() -> None:
    await init_db_pool()
    variants: list[str] = []
    for m in CASH_METHODS:
        variants.extend({m, m.upper(), m.capitalize()})
    print("variants:", variants)
    async with get_connection() as c:
        row = await c.fetchrow(SQL, RID, None, variants)
        print("cash CTE:", dict(row))
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
