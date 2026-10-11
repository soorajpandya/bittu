"""Shared read models for Route reports. Amounts stay in integer paise.

No legacy settlement batches or current-rate repricing appear in these queries.
The first four parameters are merchant, first IST date, last IST date, branch.
"""

PAYMENTS_CTE = """
WITH online AS (
    SELECT p.razorpay_payment_id AS payment_id, p.razorpay_order_id,
           p.internal_order_id, p.branch_id, p.amount_paise,
           COALESCE(a.merchant_paise, t.allocation_paise) AS merchant_paise,
           t.transfer_paise, t.transfer_id,
           p.fee_paise, p.tax_paise, p.currency, p.method,
           p.upi_vpa, p.bank_reference, p.notes,
           p.status::text AS payment_status,
           COALESCE(p.captured_at,p.created_at) AS paid_at, p.created_at,
           COALESCE(r.refunded_paise,0)::bigint AS refunded_paise,
           st.settlement_id, st.settlement_status,
           'razorpay'::text AS source
    FROM rzp_payments p
    LEFT JOIN rzp_route_allocations a
      ON a.razorpay_payment_id=p.razorpay_payment_id AND a.merchant_id=p.merchant_id
    LEFT JOIN LATERAL (
        SELECT SUM(amount_paise)::bigint AS allocation_paise,
               SUM(amount_paise) FILTER (WHERE status::text='processed')::bigint AS transfer_paise,
               MIN(transfer_id) AS transfer_id
        FROM rzp_route_transfers t
        WHERE t.razorpay_payment_id=p.razorpay_payment_id AND t.merchant_id=p.merchant_id
          AND t.status::text <> 'failed'
          AND ($3::date IS NULL OR (COALESCE(t.processed_at,t.created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3)
    ) t ON true
    LEFT JOIN LATERAL (
        SELECT SUM(amount_paise)::bigint AS refunded_paise
        FROM rzp_refunds r
        WHERE r.razorpay_payment_id=p.razorpay_payment_id AND r.merchant_id=p.merchant_id
          AND r.status='processed'
          AND ($3::date IS NULL OR (COALESCE(r.processed_at,r.created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3)
    ) r ON true
    LEFT JOIN LATERAL (
        SELECT observed.settlement_id, observed.settlement_status FROM (
        SELECT sp.settlement_id, s.status::text AS settlement_status, COALESCE(s.settled_at,s.created_at) AS observed_at
        FROM rzp_settlement_payments sp
        JOIN rzp_settlements s ON s.settlement_id=sp.settlement_id AND s.merchant_id=p.merchant_id
        WHERE sp.razorpay_payment_id=p.razorpay_payment_id AND sp.merchant_id=p.merchant_id
          AND ($3::date IS NULL OR (COALESCE(s.settled_at,sp.created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3)
        UNION ALL
        SELECT t.recipient_settlement_id, s.status::text, COALESCE(s.settled_at,s.created_at)
        FROM rzp_route_transfers t
        JOIN rzp_settlements s ON s.settlement_id=t.recipient_settlement_id AND s.merchant_id=p.merchant_id
        WHERE t.razorpay_payment_id=p.razorpay_payment_id AND t.merchant_id=p.merchant_id
          AND t.status::text='processed'
          AND ($3::date IS NULL OR (COALESCE(s.settled_at,s.created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3)
        ) observed
        ORDER BY CASE WHEN observed.settlement_status='processed' THEN 0 ELSE 1 END,
                 observed.observed_at DESC NULLS LAST
        LIMIT 1
    ) st ON true
    WHERE p.merchant_id=$1::uuid AND p.captured=true AND p.currency='INR'
      AND ($2::date IS NULL OR (COALESCE(p.captured_at,p.created_at) AT TIME ZONE 'Asia/Kolkata')::date >= $2)
      AND ($3::date IS NULL OR (COALESCE(p.captured_at,p.created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3)
      AND ($4::uuid IS NULL OR p.branch_id=$4::uuid)
), local AS (
    -- Cash and direct (merchant's own UPI/card/wallet terminal) receipts live
    -- only in `payments`: no gateway, no deduction, settled on receipt.
    -- A payment with razorpay ids is a gateway payment and is reported from
    -- the Razorpay mirror above, never here.
    SELECT p.id::text AS payment_id, NULL::text AS razorpay_order_id,
           p.order_id AS internal_order_id, p.branch_id,
           ROUND(p.amount::numeric*100)::bigint AS amount_paise,
           ROUND(p.amount::numeric*100)::bigint AS merchant_paise,
           NULL::bigint AS transfer_paise, NULL::text AS transfer_id,
           0::bigint AS fee_paise, 0::bigint AS tax_paise,
           p.currency, LOWER(p.method::text) AS method,
           NULL::text AS upi_vpa, NULL::text AS bank_reference, '{}'::jsonb AS notes,
           p.status::text AS payment_status,
           COALESCE(p.paid_at,p.created_at) AS paid_at, p.created_at,
           LEAST(
             COALESCE(r.refunded_paise,
                      CASE WHEN p.status='refunded' THEN ROUND(p.amount::numeric*100)::bigint ELSE 0 END),
             ROUND(p.amount::numeric*100)::bigint
           ) AS refunded_paise,
           NULL::text AS settlement_id, 'processed'::text AS settlement_status,
           CASE WHEN LOWER(p.method::text) IN ('cash','counter','cod') THEN 'cash' ELSE 'direct' END AS source
    FROM payments p
    LEFT JOIN LATERAL (
        SELECT SUM(ROUND(r.amount::numeric*100))::bigint AS refunded_paise
        FROM refunds r
        WHERE r.payment_id=p.id AND r.merchant_id=p.restaurant_id AND r.status='succeeded'
          AND ($3::date IS NULL OR (COALESCE(r.processed_at,r.created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3)
    ) r ON true
    WHERE p.restaurant_id=$1::uuid AND p.status IN ('completed','refunded') AND p.currency='INR'
      AND p.razorpay_payment_id IS NULL AND p.razorpay_order_id IS NULL
      AND LOWER(p.method::text) NOT IN ('online','razorpay','gateway','netbanking')
      AND ($2::date IS NULL OR (COALESCE(p.paid_at,p.created_at) AT TIME ZONE 'Asia/Kolkata')::date >= $2)
      AND ($3::date IS NULL OR (COALESCE(p.paid_at,p.created_at) AT TIME ZONE 'Asia/Kolkata')::date <= $3)
      AND ($4::uuid IS NULL OR p.branch_id=$4::uuid)
), payment_rows AS (
    SELECT *, CASE
        WHEN refunded_paise >= amount_paise THEN 'refunded'
        WHEN source IN ('cash','direct') OR settlement_status='processed' THEN 'settled'
        WHEN settlement_status IN ('failed','reversed') THEN settlement_status
        WHEN transfer_id IS NOT NULL THEN 'processing'
        ELSE 'pending' END AS settlement_state
    FROM (SELECT * FROM online UNION ALL SELECT * FROM local) p
)
"""

SETTLEMENTS_CTE = """
WITH ids AS (
    SELECT settlement_id FROM rzp_settlements WHERE merchant_id=$1::uuid
    UNION
    SELECT settlement_id FROM rzp_settlement_payments WHERE merchant_id=$1::uuid
), settlement_rows AS (
    SELECT i.settlement_id, CASE WHEN s.merchant_id=$1::uuid THEN s.utr END AS utr,
           CASE WHEN s.merchant_id=$1::uuid THEN s.status::text ELSE 'pending' END AS status,
           s.status::text AS observed_header_status,
           CASE WHEN s.merchant_id=$1::uuid THEN s.settled_at END AS settled_at,
           COALESCE(s.settled_at,s.created_at,MIN(sp.created_at)) AS created_at,
           CASE WHEN $4::uuid IS NULL AND s.merchant_id=$1::uuid THEN s.amount_paise
                ELSE COALESCE(SUM(sp.credit_paise-sp.debit_paise),0)::bigint END AS merchant_paise,
           COALESCE(SUM(sp.amount_paise) FILTER (WHERE sp.type='payment'),0)::bigint AS gross_paise,
           COALESCE(SUM(sp.amount_paise) FILTER (WHERE sp.type='refund'),0)::bigint AS refund_paise,
           COALESCE(SUM(sp.fee_paise),0)::bigint AS fee_paise,
           COALESCE(SUM(sp.tax_paise),0)::bigint AS tax_paise,
           COUNT(sp.id) FILTER (WHERE sp.type='payment')::bigint AS tx_count,
           COUNT(sp.id)::bigint AS line_count,
           COALESCE(SUM(sp.credit_paise-sp.debit_paise),0)::bigint AS reconciliation_net_paise,
           CASE WHEN s.merchant_id=$1::uuid THEN s.amount_paise END AS header_amount_paise,
           MIN(sp.created_at) AS first_seen_at
    FROM ids i
    LEFT JOIN rzp_settlements s ON s.settlement_id=i.settlement_id
    LEFT JOIN rzp_settlement_payments sp ON sp.settlement_id=i.settlement_id AND sp.merchant_id=$1::uuid
      AND ($4::uuid IS NULL OR EXISTS (
          SELECT 1 FROM rzp_payments p WHERE p.razorpay_payment_id=sp.razorpay_payment_id
            AND p.merchant_id=$1::uuid AND p.branch_id=$4::uuid
      ))
    GROUP BY i.settlement_id, s.merchant_id, s.amount_paise, s.utr, s.status, s.settled_at, s.created_at
    HAVING $4::uuid IS NULL OR COUNT(sp.id)>0
), scoped_settlements AS (
    SELECT * FROM settlement_rows
    WHERE ($2::date IS NULL OR (created_at AT TIME ZONE 'Asia/Kolkata')::date >= $2)
      AND ($3::date IS NULL OR (created_at AT TIME ZONE 'Asia/Kolkata')::date <= $3)
)
"""
