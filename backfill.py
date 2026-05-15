import asyncio
import asyncpg
import os

async def run():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not found")
        return
    # Use statement_cache_size=0 for pgbouncer compatibility
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        query = """
        WITH orphans AS (
          SELECT tsp.id              AS tsp_id,
                 tsp.session_id,
                 tsp.amount,
                 tsp.payment_method,
                 tsp.created_at,
                 o.id                AS order_id,
                 o.user_id,
                 o.branch_id,
                 o.restaurant_id
            FROM table_session_payments tsp
            JOIN dine_in_sessions dis ON dis.id = tsp.session_id
            JOIN LATERAL (
                SELECT o.id, o.user_id, o.branch_id, o.restaurant_id, o.created_at
                  FROM session_orders so
                  JOIN orders o ON o.id = so.order_id
                 WHERE so.session_id = tsp.session_id
                 ORDER BY o.created_at DESC
                 LIMIT 1
            ) o ON true
           WHERE NOT EXISTS (
                SELECT 1
                  FROM payments p
                  JOIN session_orders so2 ON so2.order_id = p.order_id
                 WHERE so2.session_id = tsp.session_id
                   AND p.amount  = tsp.amount
                   AND ABS(EXTRACT(EPOCH FROM (p.created_at - tsp.created_at))) < 86400
           )
        )
        INSERT INTO payments (id, order_id, restaurant_id, user_id, branch_id, method, status, amount, currency, paid_at, created_at)
        SELECT gen_random_uuid(),
               order_id,
               restaurant_id,
               user_id,
               branch_id,
               (CASE LOWER(payment_method)
                   WHEN 'cash' THEN 'cash'
                   WHEN 'counter' THEN 'cash'
                   WHEN 'cod' THEN 'cash'
                   WHEN 'upi' THEN 'upi'
                   WHEN 'qr' THEN 'upi'
                   WHEN 'qr_pay' THEN 'upi'
                   WHEN 'qr_code' THEN 'upi'
                   WHEN 'card' THEN 'card'
                   WHEN 'swipe' THEN 'card'
                   WHEN 'credit' THEN 'card'
                   WHEN 'debit' THEN 'card'
                   WHEN 'wallet' THEN 'wallet'
                   WHEN 'online' THEN 'online'
                   WHEN 'razorpay' THEN 'online'
                   WHEN 'gateway' THEN 'online'
                   WHEN 'netbanking' THEN 'online'
                   ELSE 'cash'
               END)::payment_method,
               'completed'::payment_status,
               amount,
               'INR',
               created_at,
               created_at
        FROM orphans
        RETURNING id, order_id, amount;
        """
        rows = await conn.fetch(query)
        print(f"Inserted {len(rows)} records.")
        for r in rows:
            print(f"ID: {r['id']}, Order ID: {r['order_id']}, Amount: {r['amount']}")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await conn.close()

asyncio.run(run())
