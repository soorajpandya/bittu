"""
Statement & Settlement Service
═══════════════════════════════════════════════════════════════════════════════
Merchant-facing settlement experience — Razorpay/PhonePe-style dashboard
inside Bittu POS.

Architecture (5 % gross-deduction model)
─────────────────────────────────────────────────────────────────────────────
A flat 5.00 % of every collected payment is deducted from the merchant's
gross. They receive exactly (gross × 0.95) in their bank account.

That 5 % cut is split three ways:

  1. Razorpay server-side cut — 0.99 % + 18 % GST  ≈ 1.1682 % of gross.
     Razorpay intercepts this BEFORE settling the remainder to our
     pooled account, so we never "own" these paisa.
  2. Platform pool — what's left of the 5 % after Razorpay's slice
     ≈ 3.8318 % of gross. GST-inclusive.
  3. The pool is reverse-extracted into our base platform fee
     (pool ÷ 1.18  ≈ 3.2473 % of gross) and the GST component on it
     (residual "plug"  ≈ 0.5845 % of gross). Computing GST as the
     residual guarantees  bittu_fee + gst_on_fee == platform_pool  at
     paisa precision — no rounding drift across batches.

  Gross Amount (collected from customers)
  – Razorpay PG cut       (gross × 1.1682 %)   ← intercepted server-side
  – Bittu Platform Fee    (pool  ÷ 1.18)
  – GST on Platform Fee   (pool  − bittu_fee)
  = Net Settlement Amount (gross × 95.00 %)    ← credited to merchant bank

Invariant per settlement transaction:
  razorpay_cut + bittu_fee + gst_on_fee + net_to_merchant == gross

Key design decisions:
  • Decimal-safe arithmetic via Python's Decimal — no floats
  • Idempotent settlement creation (unique payment_id per transaction)
  • Immutable timeline entries — status history is append-only
  • All money mutations inside SERIALIZABLE transactions
  • Full audit trail via activity_logs on every state change
  • Accounting entries generated via existing accounting_engine (reuse)

Tables:
  bittu_settlements               — settlement batches
  bittu_settlement_transactions   — per-payment breakdown inside a batch
  bittu_settlement_timeline       — immutable event log per settlement

Integrates with:
  payments, orders                — source data
  accounting_engine               — journal entries when settled
  activity_logs                   — RBAC audit trail
  daily_closings                  — settlement figures for daily close
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import json as _json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from uuid import UUID, uuid4

from app.core.database import get_connection, get_serializable_transaction
from app.core.exceptions import NotFoundError, ValidationError, ConflictError
from app.core.ist import ist_today
from app.core.logging import get_logger
from app.services.accounting_engine import accounting_engine
from app.services.activity_log_service import log_activity
from app.services.merchant_ledger_integration import (
    post_settlement_settled,
    post_settlement_reversed,
)
from app.services.escrow_integration import release_holds_for_settlement

logger = get_logger(__name__)

# ── Fee constants ─────────────────────────────────────────────────────────────
# 5 % flat gross deduction. Customer pays gross, merchant nets gross × 0.95.
TOTAL_DEDUCTION_RATE = Decimal("0.050000")   # 5.00 % flat gross deduction
GST_RATE             = Decimal("0.180000")   # 18 % GST

# Razorpay's automatic server-side cut: 0.99 % base + 18 % GST on that base.
# Computed inline so the relationship stays self-documenting; equivalent to
# Decimal("0.011682").
RAZORPAY_TOTAL_RATE  = (
    Decimal("0.009900") * (Decimal("1.000000") + GST_RATE)
)  # = 0.011682  (1.1682 % of gross)

# Remainder of the 5 % cut after Razorpay's slice = our gross platform pool.
# The pool is GST-inclusive; reverse-extract the base platform fee, then
# treat GST as the residual so the three components reconcile exactly.
PLATFORM_POOL_RATE   = TOTAL_DEDUCTION_RATE - RAZORPAY_TOTAL_RATE  # 0.038318
BITTU_FEE_RATE       = (PLATFORM_POOL_RATE / (Decimal("1") + GST_RATE)).quantize(
    Decimal("0.000001"), rounding=ROUND_HALF_UP
)  # ≈ 0.032473  (3.2473 % of gross)


def _q2(val) -> Decimal:
    """Quantize to 2 decimal places — bank-facing (paisa) amounts."""
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _q4(val) -> Decimal:
    """Quantize to 4 decimal places — internal revenue-split components."""
    return Decimal(str(val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _q6(val) -> Decimal:
    """Quantize to 6 decimal places — for rate constants only."""
    return Decimal(str(val)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _calc_settlement_breakdown(gross) -> dict:
    """
    Full per-settlement breakdown under the 5 % gross-deduction model.

    Bank-facing amounts (`total_deduction`, `net_to_merchant`) are quantized
    to paisa (2 dp) — they hit the merchant's bank statement and must match
    exactly. Internal revenue-split components (`razorpay_cut`, `bittu_fee`,
    `gst_on_fee`) are held at 4 dp so we don't lose precision when
    reconciling against Razorpay's own settlement files, while still
    summing back to `total_deduction` at paisa precision.

    Invariants (verified at 4 dp; trivially round-trip at 2 dp):
        razorpay_cut + bittu_fee + gst_on_fee == total_deduction
        net_to_merchant + total_deduction      == gross

    Worked example for gross = ₹100.00:
        total_deduction = 5.0000   net_to_merchant = 95.0000
        razorpay_cut    = 1.1682   platform_pool   = 3.8318
        bittu_fee       = 3.2473   gst_on_fee      = 0.5845
    """
    gross = _q2(gross)

    # Bank-facing — exact at paisa.
    total_deduction = _q2(gross * TOTAL_DEDUCTION_RATE)
    net_to_merchant = _q2(gross - total_deduction)

    # Razorpay intercepts this server-side; we never receive these paisa.
    razorpay_cut    = _q4(gross * RAZORPAY_TOTAL_RATE)

    # What lands in our pooled account, GST-inclusive. Anchored on the
    # already-paisa-quantized `total_deduction` so the three components
    # always add back up to it exactly.
    platform_pool   = _q4(_q4(total_deduction) - razorpay_cut)

    # Reverse-extract base platform fee, then plug GST as the residual.
    bittu_fee       = _q4(platform_pool / (Decimal("1") + GST_RATE))
    gst_on_fee      = _q4(platform_pool - bittu_fee)

    return {
        "gross":            gross,
        "total_deduction":  total_deduction,
        "net_to_merchant":  net_to_merchant,
        "razorpay_cut":     razorpay_cut,
        "platform_pool":    platform_pool,
        "bittu_fee":        bittu_fee,
        "gst_on_fee":       gst_on_fee,
    }


def _calc_fee(gross) -> tuple[Decimal, Decimal, Decimal]:
    """
    Back-compat tuple: ``(bittu_fee, gst_on_fee, net_to_merchant)``.

    Returns ONLY our platform share — the Razorpay-intercepted cut is
    NOT folded into ``bittu_fee`` or ``gst_on_fee``. Callers that need
    the full split (e.g. settlement-create routes, reconciliation jobs)
    should call :func:`_calc_settlement_breakdown` instead.
    """
    br = _calc_settlement_breakdown(gross)
    return br["bittu_fee"], br["gst_on_fee"], br["net_to_merchant"]


def _make_reference(restaurant_id: str) -> str:
    """Generate a human-readable settlement reference like STL-20260506-XXXX."""
    today = ist_today().strftime("%Y%m%d")
    suffix = str(uuid4())[:8].upper()
    return f"STL-{today}-{suffix}"


def _expected_eta(cycle: str) -> datetime:
    """Return the expected settlement time based on cycle."""
    now = datetime.now(timezone.utc)
    if cycle == "T+0":
        # Same day at 7 PM IST (13:30 UTC)
        eta_today = now.replace(hour=13, minute=30, second=0, microsecond=0)
        return eta_today if now < eta_today else eta_today + timedelta(days=1)
    else:
        # T+1: next working day at 7 PM IST
        return (now + timedelta(days=1)).replace(hour=13, minute=30, second=0, microsecond=0)


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, Decimal):
            d[k] = float(v)
        elif isinstance(v, (datetime, date)):
            d[k] = v.isoformat()
        elif isinstance(v, UUID):
            d[k] = str(v)
    return d


class StatementService:
    """
    Powers all /api/v1/statements/* endpoints.
    Singleton — import `statement_service` at module level.
    """

    # ════════════════════════════════════════════════════════════════════════
    # SUMMARY DASHBOARD
    # ════════════════════════════════════════════════════════════════════════

    async def get_summary(
        self,
        restaurant_id: str,
        branch_id: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ) -> dict:
        """
        Top-level dashboard summary.
        Primary source: payments table (actual money collected).
        Settlement overlay: bittu_settlements (populated as batches are processed).
        """
        rid = UUID(restaurant_id)
        bid = UUID(branch_id) if branch_id else None
        today = ist_today()
        from_d = from_date or date(today.year, today.month, 1)
        to_d   = to_date   or today

        logger.info("statement_summary_query",
                    restaurant_id=str(rid),
                    branch_id=str(bid) if bid else None,
                    from_d=str(from_d), to_d=str(to_d))

        base_params: list = [rid, from_d, to_d]
        p_branch = ""
        s_branch = ""
        if bid:
            base_params.append(bid)
            p_branch = "AND p.branch_id = $4"
            s_branch = "AND bs.branch_id = $4"

        async with get_connection() as conn:
            # ── Primary: actual money collected from payments ──────────────
            pay_agg = await conn.fetchrow(f"""
                SELECT
                    COALESCE(SUM(p.amount), 0) AS total_received
                FROM payments p
                WHERE p.restaurant_id = $1
                  AND DATE(p.created_at AT TIME ZONE 'Asia/Kolkata') BETWEEN $2 AND $3
                  AND p.status = 'completed'
                  {p_branch}
            """, *base_params)

            # today_collection is always for TODAY regardless of date-range filter
            today_params: list = [rid]
            today_branch = ""
            if bid:
                today_params.append(bid)
                today_branch = "AND p.branch_id = $2"
            today_row = await conn.fetchrow(f"""
                SELECT COALESCE(SUM(p.amount), 0) AS today_collection
                FROM payments p
                WHERE p.restaurant_id = $1
                  AND DATE(p.created_at AT TIME ZONE 'Asia/Kolkata') = (now() AT TIME ZONE 'Asia/Kolkata')::date
                  AND p.status = 'completed'
                  {today_branch}
            """, *today_params)

            # ── Secondary: settled / fee totals from bittu_settlements ─────
            stl_agg = await conn.fetchrow(f"""
                SELECT
                    COALESCE(SUM(bs.net_settlement_amount)
                        FILTER (WHERE bs.settlement_status = 'settled'), 0)     AS settled_amount,
                    COALESCE(SUM(bs.bittu_fee_amount), 0)                       AS total_bittu_charges,
                    COALESCE(SUM(bs.gst_amount), 0)                             AS gst_on_charges,
                    COALESCE(SUM(bs.razorpay_cut_amount), 0)                    AS razorpay_charges
                FROM bittu_settlements bs
                WHERE bs.restaurant_id = $1
                  AND DATE(bs.created_at AT TIME ZONE 'Asia/Kolkata') BETWEEN $2 AND $3
                  AND bs.settlement_status != 'reversed'
                  {s_branch}
            """, *base_params)

            # ── Next pending settlement ETA ────────────────────────────────
            eta_params: list = [rid]
            if bid:
                eta_params.append(bid)
            next_settlement = await conn.fetchrow(f"""
                SELECT id, settlement_reference, net_settlement_amount,
                       expected_settlement_at, settlement_status, settlement_cycle
                FROM bittu_settlements
                WHERE restaurant_id = $1
                  AND settlement_status IN ('pending', 'processing', 'sent_to_bank')
                  {"AND branch_id = $2" if bid else ""}
                ORDER BY expected_settlement_at ASC NULLS LAST
                LIMIT 1
            """, *eta_params)

        total_received      = float(pay_agg["total_received"])
        today_collection    = float(today_row["today_collection"])
        settled_amount      = float(stl_agg["settled_amount"])
        total_bittu_charges = float(stl_agg["total_bittu_charges"])
        gst_on_charges      = float(stl_agg["gst_on_charges"])
        razorpay_charges    = float(stl_agg["razorpay_charges"])
        # Headline merchant-facing deduction = full 5 % cut
        # = platform fee + GST + Razorpay's intercepted slice.
        total_deductions    = total_bittu_charges + gst_on_charges + razorpay_charges
        pending_settlement  = max(0.0, total_received - settled_amount)
        net_amount_credited = settled_amount

        # ── Build ETA message ──────────────────────────────────────────────
        upcoming_eta = None
        upcoming_eta_message = None
        upcoming_amount = None
        ist_offset = timedelta(hours=5, minutes=30)

        if next_settlement:
            eta_ts = next_settlement["expected_settlement_at"]
            amt    = float(next_settlement["net_settlement_amount"])
            upcoming_amount = amt
            if eta_ts:
                eta_utc = eta_ts if eta_ts.tzinfo else eta_ts.replace(tzinfo=timezone.utc)
                eta_ist = eta_utc + ist_offset
                now_ist = datetime.now(timezone.utc) + ist_offset
                if eta_ist.date() == now_ist.date():
                    day_label = "Today"
                elif eta_ist.date() == (now_ist + timedelta(days=1)).date():
                    day_label = "Tomorrow"
                else:
                    day_label = eta_ist.strftime("%b %d")
                time_label = eta_ist.strftime("%I:%M %p").lstrip("0")
                upcoming_eta_message = (
                    f"₹{amt:,.2f} will be settled to your bank account"
                    f" by {day_label} {time_label}"
                )
                upcoming_eta = eta_ts.isoformat()
            else:
                upcoming_eta_message = f"₹{amt:,.2f} settlement processing"
        elif pending_settlement > 0:
            # No batch created yet — derive ETA from pending payment amount
            eta = _expected_eta("T+1")
            eta_ist = eta + ist_offset
            now_ist = datetime.now(timezone.utc) + ist_offset
            day_label = "Today" if eta_ist.date() == now_ist.date() else "Tomorrow"
            time_label = eta_ist.strftime("%I:%M %p").lstrip("0")
            upcoming_eta_message = (
                f"₹{pending_settlement:,.2f} will be settled to your bank account"
                f" by {day_label} {time_label}"
            )
            upcoming_eta = eta.isoformat()
            upcoming_amount = pending_settlement

        return {
            "period": {"from": from_d.isoformat(), "to": to_d.isoformat()},
            "total_received":       total_received,
            "today_collection":     today_collection,
            "pending_settlement":   pending_settlement,
            "settled_amount":       settled_amount,
            "total_bittu_charges":  total_bittu_charges,
            "gst_on_charges":       gst_on_charges,
            "razorpay_charges":     razorpay_charges,
            "total_deductions":     total_deductions,
            "net_amount_credited":  net_amount_credited,
            "upcoming_settlement": {
                "eta":      upcoming_eta,
                "amount":   upcoming_amount,
                "message":  upcoming_eta_message,
            },
            "fee_info": {
                "total_deduction_rate_pct": "5.00%",
                "razorpay_cut_rate_pct":    "1.1682%",
                "bittu_fee_rate_pct":       "3.2473%",
                "gst_rate_pct":             "18%",
                "description": (
                    "5% flat gross deduction. Razorpay auto-deducts 1.1682% "
                    "(0.99% + 18% GST); the remainder is Bittu's platform fee "
                    "(reverse-GST extracted) and the GST on that fee."
                ),
            },
        }

    # ════════════════════════════════════════════════════════════════════════
    # TRANSACTIONS LIST
    # ════════════════════════════════════════════════════════════════════════

    async def get_transactions(
        self,
        restaurant_id: str,
        branch_id: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        settlement_status: Optional[str] = None,
        payment_method: Optional[str] = None,
        search: Optional[str] = None,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """
        Paginated transaction list sourced directly from the payments table.
        Each completed payment = one transaction row with fee preview calculated in SQL.
        Settlement metadata (status, batch reference) overlaid from
        bittu_settlement_transactions when available.
        """
        rid = UUID(restaurant_id)
        bid = UUID(branch_id) if branch_id else None
        today = ist_today()
        from_d = from_date or (today - timedelta(days=30))
        to_d   = to_date   or today

        params: list = [rid, from_d, to_d]
        conditions = [
            "p.restaurant_id = $1",
            "DATE(p.created_at AT TIME ZONE 'Asia/Kolkata') BETWEEN $2 AND $3",
            "p.status = 'completed'",
        ]
        idx = 4

        if bid:
            conditions.append(f"p.branch_id = ${idx}")
            params.append(bid)
            idx += 1

        if settlement_status:
            conditions.append(f"COALESCE(bst.settlement_status, 'pending') = ${idx}")
            params.append(settlement_status)
            idx += 1

        if payment_method:
            conditions.append(f"p.method = ${idx}")
            params.append(payment_method)
            idx += 1

        if search:
            conditions.append(
                f"(c.name ILIKE ${idx} OR p.id::text ILIKE ${idx} "
                f"OR p.order_id::text ILIKE ${idx})"
            )
            params.append(f"%{search}%")
            idx += 1

        where_clause = " AND ".join(conditions)

        # net_amount is proportional to gross — same sort order; avoids subquery
        sort_col_map = {
            "created_at":     "p.created_at",
            "gross_amount":   "p.amount",
            "net_amount":     "p.amount",
            "payment_method": "p.method",
        }
        order_expr     = sort_col_map.get(sort_by, "p.created_at")
        sort_direction = "ASC" if sort_dir.lower() == "asc" else "DESC"

        count_params = params[:]
        data_params  = params + [limit, offset]

        async with get_connection() as conn:
            total = await conn.fetchval(f"""
                SELECT COUNT(*)
                FROM payments p
                LEFT JOIN orders o ON o.id = p.order_id
                LEFT JOIN customers c ON c.id = o.customer_id
                LEFT JOIN bittu_settlement_transactions bst
                    ON bst.payment_id = p.id AND bst.transaction_type = 'payment'
                WHERE {where_clause}
            """, *count_params)

            rows = await conn.fetch(f"""
                SELECT
                    p.id                                                              AS id,
                    p.id                                                              AS payment_id,
                    p.order_id,
                    p.amount                                                          AS gross_amount,
                    ROUND(p.amount::numeric * 0.001500, 6)                           AS fee_amount,
                    ROUND(p.amount::numeric * 0.001500 * 0.180000, 6)               AS gst_amount,
                    ROUND(
                        p.amount::numeric
                        - ROUND(p.amount::numeric * 0.001500, 6)
                        - ROUND(p.amount::numeric * 0.001500 * 0.180000, 6),
                        2
                    )                                                                 AS net_amount,
                    'payment'                                                         AS transaction_type,
                    p.method                                                          AS payment_method,
                    c.name                                                            AS customer_name,
                    p.order_id::text                                                  AS order_reference,
                    COALESCE(bst.settlement_status, 'pending')                        AS settlement_status,
                    bst.settlement_id,
                    p.created_at,
                    bs.expected_settlement_at,
                    bs.settlement_reference,
                    bs.settlement_cycle
                FROM payments p
                LEFT JOIN orders o ON o.id = p.order_id
                LEFT JOIN customers c ON c.id = o.customer_id
                LEFT JOIN bittu_settlement_transactions bst
                    ON bst.payment_id = p.id AND bst.transaction_type = 'payment'
                LEFT JOIN bittu_settlements bs ON bs.id = bst.settlement_id
                WHERE {where_clause}
                ORDER BY {order_expr} {sort_direction}
                LIMIT ${idx} OFFSET ${idx + 1}
            """, *data_params)

        return {
            "total":  total,
            "limit":  limit,
            "offset": offset,
            "items":  [_row_to_dict(r) for r in rows],
        }

    # ════════════════════════════════════════════════════════════════════════
    # SETTLEMENTS LIST
    # ════════════════════════════════════════════════════════════════════════

    async def get_settlements(
        self,
        restaurant_id: str,
        branch_id: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        status: Optional[str] = None,
        cycle: Optional[str] = None,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """
        Paginated settlement batch list.

        Returns a UNION of:
          1. Real bittu_settlements (populated after batches are processed)
          2. Virtual daily payment groups — payments not yet in any settlement
             batch, grouped by calendar date, shown as status='pending'

        This ensures the list is never empty while the settlement engine
        processes existing payments over time.
        """
        rid   = UUID(restaurant_id)
        bid   = UUID(branch_id) if branch_id else None
        today = ist_today()
        from_d = from_date or (today - timedelta(days=30))
        to_d   = to_date   or today

        base_params: list = [rid, from_d, to_d]
        idx = 4

        real_branch = ""
        virt_branch = ""
        if bid:
            real_branch = f"AND bs.branch_id = ${idx}"
            virt_branch = f"AND p.branch_id  = ${idx}"
            base_params.append(bid)
            idx += 1

        outer_filters: list[str] = []
        if status:
            outer_filters.append(f"settlement_status = ${idx}")
            base_params.append(status)
            idx += 1
        if cycle:
            outer_filters.append(f"settlement_cycle = ${idx}")
            base_params.append(cycle)
            idx += 1
        outer_where = ("WHERE " + " AND ".join(outer_filters)) if outer_filters else ""

        allowed_sort = {"created_at", "gross_amount", "net_settlement_amount"}
        sort_col = sort_by if sort_by in allowed_sort else "created_at"
        sort_direction = "ASC" if sort_dir.lower() == "asc" else "DESC"

        count_params = base_params[:]
        data_params  = base_params + [limit, offset]

        # ── Combined CTE: real + virtual settlements ──────────────────────
        union_sql = f"""
            WITH combined AS (
                -- Real Bittu settlement batches
                SELECT
                    bs.id::text                  AS id,
                    bs.settlement_reference,
                    bs.gross_amount::numeric     AS gross_amount,
                    bs.bittu_fee_amount::numeric AS bittu_fee_amount,
                    bs.gst_amount::numeric       AS gst_amount,
                    bs.net_settlement_amount::numeric AS net_settlement_amount,
                    bs.settlement_status,
                    bs.settlement_cycle,
                    bs.expected_settlement_at,
                    bs.settled_at,
                    bs.bank_reference_number,
                    bs.created_at,
                    COUNT(bst.id)::bigint        AS transaction_count,
                    COUNT(bst.id)
                        FILTER (WHERE bst.transaction_type = 'refund')::bigint
                                                 AS refund_count,
                    false                        AS is_virtual
                FROM bittu_settlements bs
                LEFT JOIN bittu_settlement_transactions bst ON bst.settlement_id = bs.id
                WHERE bs.restaurant_id = $1
                  AND DATE(bs.created_at AT TIME ZONE 'Asia/Kolkata') BETWEEN $2 AND $3
                  {real_branch}
                GROUP BY bs.id

                UNION ALL

                -- Virtual daily groups: payments not yet in any settlement batch
                SELECT
                    ('VIRT-' || DATE(p.created_at AT TIME ZONE 'Asia/Kolkata')::text)          AS id,
                    ('PENDING-' || DATE(p.created_at AT TIME ZONE 'Asia/Kolkata')::text)        AS settlement_reference,
                    SUM(p.amount::numeric)                          AS gross_amount,
                    ROUND(SUM(p.amount::numeric) * 0.001500, 6)    AS bittu_fee_amount,
                    ROUND(SUM(p.amount::numeric) * 0.001500
                          * 0.180000, 6)                            AS gst_amount,
                    ROUND(
                        SUM(p.amount::numeric)
                        - ROUND(SUM(p.amount::numeric) * 0.001500, 6)
                        - ROUND(SUM(p.amount::numeric) * 0.001500 * 0.180000, 6),
                        2
                    )                                               AS net_settlement_amount,
                    'pending'                                        AS settlement_status,
                    'T+1'                                            AS settlement_cycle,
                    NULL::timestamptz                                AS expected_settlement_at,
                    NULL::timestamptz                                AS settled_at,
                    NULL::text                                       AS bank_reference_number,
                    MIN(p.created_at)                                AS created_at,
                    COUNT(*)::bigint                                 AS transaction_count,
                    0::bigint                                        AS refund_count,
                    true                                             AS is_virtual
                FROM payments p
                WHERE p.restaurant_id = $1
                  AND DATE(p.created_at AT TIME ZONE 'Asia/Kolkata') BETWEEN $2 AND $3
                  AND p.status = 'completed'
                  {virt_branch}
                  AND NOT EXISTS (
                      SELECT 1 FROM bittu_settlement_transactions bst2
                      WHERE bst2.payment_id = p.id
                        AND bst2.transaction_type = 'payment'
                  )
                GROUP BY DATE(p.created_at AT TIME ZONE 'Asia/Kolkata')
                HAVING SUM(p.amount) > 0
            )
        """

        async with get_connection() as conn:
            total = await conn.fetchval(
                union_sql + f"SELECT COUNT(*) FROM combined {outer_where}",
                *count_params,
            )

            rows = await conn.fetch(
                union_sql + f"""
                SELECT * FROM combined
                {outer_where}
                ORDER BY {sort_col} {sort_direction}
                LIMIT ${idx} OFFSET ${idx + 1}
                """,
                *data_params,
            )

        return {
            "total":  total,
            "limit":  limit,
            "offset": offset,
            "items":  [_row_to_dict(r) for r in rows],
        }

    # ════════════════════════════════════════════════════════════════════════
    # SETTLEMENT DETAIL
    # ════════════════════════════════════════════════════════════════════════

    async def get_settlement_detail(
        self,
        settlement_id: str,
        restaurant_id: str,
    ) -> dict:
        """
        Full settlement detail: batch info + all transactions + timeline.
        """
        sid = UUID(settlement_id)
        rid = UUID(restaurant_id)

        async with get_connection() as conn:
            settlement = await conn.fetchrow("""
                SELECT bs.*,
                       COUNT(bst.id) AS transaction_count
                FROM bittu_settlements bs
                LEFT JOIN bittu_settlement_transactions bst ON bst.settlement_id = bs.id
                WHERE bs.id = $1 AND bs.restaurant_id = $2
                GROUP BY bs.id
            """, sid, rid)

            if not settlement:
                raise NotFoundError("Settlement", settlement_id)

            transactions = await conn.fetch("""
                SELECT * FROM bittu_settlement_transactions
                WHERE settlement_id = $1
                ORDER BY created_at ASC
            """, sid)

            timeline = await conn.fetch("""
                SELECT * FROM bittu_settlement_timeline
                WHERE settlement_id = $1
                ORDER BY occurred_at ASC
            """, sid)

        return {
            "settlement":    _row_to_dict(settlement),
            "transactions":  [_row_to_dict(r) for r in transactions],
            "timeline":      [_row_to_dict(r) for r in timeline],
            "breakdown": {
                "gross_amount":         float(settlement["gross_amount"]),
                "bittu_fee_amount":     float(settlement["bittu_fee_amount"]),
                "gst_amount":           float(settlement["gst_amount"]),
                "net_settlement_amount": float(settlement["net_settlement_amount"]),
                "fee_rate_pct":         "0.2542%",
                "gst_rate_pct":         "18%",
                "formula": (
                    f"₹{float(settlement['gross_amount']):,.2f} "
                    f"- ₹{float(settlement['bittu_fee_amount']):,.2f} (fee) "
                    f"- ₹{float(settlement['gst_amount']):,.2f} (GST) "
                    f"= ₹{float(settlement['net_settlement_amount']):,.2f}"
                ),
            },
        }

    # ════════════════════════════════════════════════════════════════════════
    # PENDING SETTLEMENTS
    # ════════════════════════════════════════════════════════════════════════

    async def get_pending(
        self,
        restaurant_id: str,
        branch_id: Optional[str] = None,
    ) -> dict:
        """
        All pending/processing/sent-to-bank settlements with ETA.
        Used for the "Pending" tab on mobile.
        """
        rid = UUID(restaurant_id)
        bid = UUID(branch_id) if branch_id else None
        branch_clause = "AND branch_id = $2" if bid else ""
        params: list = [rid]
        if bid:
            params.append(bid)

        async with get_connection() as conn:
            rows = await conn.fetch(f"""
                SELECT bs.*,
                       COUNT(bst.id) AS transaction_count
                FROM bittu_settlements bs
                LEFT JOIN bittu_settlement_transactions bst ON bst.settlement_id = bs.id
                WHERE bs.restaurant_id = $1
                  AND bs.settlement_status IN ('pending', 'processing', 'sent_to_bank')
                  {branch_clause}
                GROUP BY bs.id
                ORDER BY bs.expected_settlement_at ASC NULLS LAST
            """, *params)

            total_pending = await conn.fetchval(f"""
                SELECT COALESCE(SUM(net_settlement_amount), 0)
                FROM bittu_settlements
                WHERE restaurant_id = $1
                  AND settlement_status IN ('pending', 'processing', 'sent_to_bank')
                  {branch_clause}
            """, *params)

        items = [_row_to_dict(r) for r in rows]
        return {
            "total_pending_amount": float(total_pending),
            "count":  len(items),
            "items":  items,
        }

    # ════════════════════════════════════════════════════════════════════════
    # SETTLEMENT TIMELINE
    # ════════════════════════════════════════════════════════════════════════

    async def get_settlement_timeline(
        self,
        settlement_id: str,
        restaurant_id: str,
    ) -> list[dict]:
        """Ordered timeline of all events for a settlement."""
        sid = UUID(settlement_id)
        rid = UUID(restaurant_id)

        async with get_connection() as conn:
            # Verify ownership
            exists = await conn.fetchval(
                "SELECT 1 FROM bittu_settlements WHERE id = $1 AND restaurant_id = $2",
                sid, rid,
            )
            if not exists:
                raise NotFoundError("Settlement", settlement_id)

            rows = await conn.fetch("""
                SELECT * FROM bittu_settlement_timeline
                WHERE settlement_id = $1
                ORDER BY occurred_at ASC
            """, sid)

        return [_row_to_dict(r) for r in rows]

    # ════════════════════════════════════════════════════════════════════════
    # EXPORT
    # ════════════════════════════════════════════════════════════════════════

    async def export_statement(
        self,
        restaurant_id: str,
        branch_id: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        settlement_status: Optional[str] = None,
    ) -> dict:
        """
        Structured export payload consumed by the frontend to generate
        PDF or Excel. Contains:
          - summary section
          - settlement-by-settlement breakdown
          - all transactions with full detail
          - fee deduction totals
        """
        rid = UUID(restaurant_id)
        bid = UUID(branch_id) if branch_id else None
        today = ist_today()
        from_d = from_date or date(today.year, today.month, 1)
        to_d   = to_date   or today

        branch_clause = "AND bs.branch_id = $4" if bid else ""
        params: list = [rid, from_d, to_d]
        if bid:
            params.append(bid)

        status_clause = ""
        if settlement_status:
            status_clause = f"AND bs.settlement_status = ${len(params) + 1}"
            params.append(settlement_status)

        async with get_connection() as conn:
            # Fetch restaurant name
            restaurant = await conn.fetchrow(
                "SELECT name FROM restaurants WHERE id = $1", rid,
            )

            settlements = await conn.fetch(f"""
                SELECT bs.*,
                       COUNT(bst.id) AS transaction_count
                FROM bittu_settlements bs
                LEFT JOIN bittu_settlement_transactions bst ON bst.settlement_id = bs.id
                WHERE bs.restaurant_id = $1
                  AND DATE(bs.created_at AT TIME ZONE 'Asia/Kolkata') BETWEEN $2 AND $3
                  {branch_clause}
                  {status_clause}
                GROUP BY bs.id
                ORDER BY bs.created_at DESC
            """, *params)

            transactions = await conn.fetch(f"""
                SELECT bst.*
                FROM bittu_settlement_transactions bst
                WHERE bst.restaurant_id = $1
                  AND DATE(bst.created_at) BETWEEN $2 AND $3
                  {"AND bst.branch_id = $4" if bid else ""}
                ORDER BY bst.created_at DESC
            """, *params[:4] if bid else params[:3])

        all_settlements = [_row_to_dict(r) for r in settlements]
        all_transactions = [_row_to_dict(r) for r in transactions]

        # Aggregate totals
        total_gross      = sum(float(r["gross_amount"])           for r in settlements)
        total_fee        = sum(float(r["bittu_fee_amount"])        for r in settlements)
        total_gst        = sum(float(r["gst_amount"])             for r in settlements)
        total_net        = sum(float(r["net_settlement_amount"])  for r in settlements)
        total_settled    = sum(
            float(r["net_settlement_amount"])
            for r in settlements if r["settlement_status"] == "settled"
        )
        total_pending    = sum(
            float(r["net_settlement_amount"])
            for r in settlements if r["settlement_status"] in ("pending", "processing", "sent_to_bank")
        )

        return {
            "export_meta": {
                "restaurant_name": restaurant["name"] if restaurant else "",
                "period_from":  from_d.isoformat(),
                "period_to":    to_d.isoformat(),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "total_records": len(all_transactions),
            },
            "summary": {
                "total_gross_collected":  total_gross,
                "total_bittu_fee":        total_fee,
                "total_gst_on_fee":       total_gst,
                "total_net_settlement":   total_net,
                "total_settled":          total_settled,
                "total_pending":          total_pending,
                "settlement_count":       len(all_settlements),
                "transaction_count":      len(all_transactions),
            },
            "settlements":   all_settlements,
            "transactions":  all_transactions,
            "columns": {
                "transactions": [
                    "order_reference", "payment_id", "customer_name",
                    "gross_amount", "fee_amount", "gst_amount", "net_amount",
                    "payment_method", "settlement_status", "created_at",
                    "settlement_reference",
                ],
            },
        }

    # ════════════════════════════════════════════════════════════════════════
    # ENQUEUE PAYMENT FOR SETTLEMENT  (called by payment_service on completion)
    # ════════════════════════════════════════════════════════════════════════

    async def enqueue_payment_for_settlement(
        self,
        *,
        payment_id: str,
        order_id: str,
        restaurant_id: str,
        branch_id: Optional[str] = None,
        gross_amount: float,
        payment_method: str = "upi",
        customer_name: Optional[str] = None,
        order_reference: Optional[str] = None,
        cycle: str = "T+1",
        actor_id: str = "system",
    ) -> dict:
        """
        Called after a payment is completed.
        Creates or finds today's settlement batch for the restaurant, then
        appends a new transaction line.

        Idempotent: the unique index on (payment_id, transaction_type='payment')
        prevents double-processing of the same payment.
        """
        rid = UUID(restaurant_id)
        bid = UUID(branch_id) if branch_id else None
        pid = UUID(payment_id)
        oid = UUID(order_id)

        gross = _q2(gross_amount)
        br = _calc_settlement_breakdown(gross)
        bittu_fee    = br["bittu_fee"]
        gst_on_fee   = br["gst_on_fee"]
        net          = br["net_to_merchant"]
        razorpay_cut = br["razorpay_cut"]

        # Idempotency: bail out if this payment is already settled/enqueued
        async with get_connection() as conn:
            existing = await conn.fetchval(
                """
                SELECT id FROM bittu_settlement_transactions
                WHERE payment_id = $1 AND transaction_type = 'payment'
                """,
                pid,
            )
            if existing:
                logger.info("settlement_enqueue_idempotent", payment_id=payment_id)
                return {"settlement_transaction_id": str(existing), "status": "already_queued"}

        # Find or create today's open settlement batch for this branch
        today = ist_today()
        idem_key = f"batch_{restaurant_id}_{branch_id or 'main'}_{today.isoformat()}"
        eta = _expected_eta(cycle)

        async with get_serializable_transaction() as conn:
            # Find existing PENDING batch for today
            batch = await conn.fetchrow("""
                SELECT id, gross_amount, bittu_fee_amount, gst_amount,
                       net_settlement_amount, razorpay_cut_amount
                FROM bittu_settlements
                WHERE restaurant_id = $1
                  AND settlement_status = 'pending'
                  AND DATE(created_at) = $2
                  AND COALESCE(branch_id::text, '') = $3
                FOR UPDATE
            """, rid, today, str(bid) if bid else "")

            if batch:
                # Append to existing batch
                batch_id = batch["id"]
                new_gross = _q2(Decimal(str(batch["gross_amount"])) + gross)
                new_fee   = _q6(Decimal(str(batch["bittu_fee_amount"])) + bittu_fee)
                new_gst   = _q6(Decimal(str(batch["gst_amount"])) + gst_on_fee)
                new_net   = _q2(Decimal(str(batch["net_settlement_amount"])) + net)
                new_rzp   = _q6(Decimal(str(batch["razorpay_cut_amount"])) + razorpay_cut)

                await conn.execute("""
                    UPDATE bittu_settlements
                    SET gross_amount          = $2,
                        bittu_fee_amount      = $3,
                        gst_amount            = $4,
                        net_settlement_amount = $5,
                        razorpay_cut_amount   = $6,
                        expected_settlement_at = $7,
                        updated_at            = NOW()
                    WHERE id = $1
                """, batch_id, float(new_gross), float(new_fee), float(new_gst),
                    float(new_net), float(new_rzp), eta)
            else:
                # Create new batch
                ref = _make_reference(restaurant_id)
                batch_id = await conn.fetchval("""
                    INSERT INTO bittu_settlements (
                        restaurant_id, branch_id, settlement_reference,
                        gross_amount, bittu_fee_amount, gst_amount,
                        net_settlement_amount, razorpay_cut_amount,
                        fee_rate, gst_rate, settlement_cycle, settlement_status,
                        expected_settlement_at, idempotency_key,
                        period_start, period_end
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8,
                        $9, $10, $11, 'pending',
                        $12, $13,
                        $14, $14
                    )
                    RETURNING id
                """,
                    rid, bid, ref,
                    float(gross), float(bittu_fee), float(gst_on_fee),
                    float(net), float(razorpay_cut),
                    float(BITTU_FEE_RATE), float(GST_RATE), cycle,
                    eta, idem_key,
                    datetime.now(timezone.utc),
                )
                # Log creation event to timeline
                await conn.execute("""
                    INSERT INTO bittu_settlement_timeline
                        (settlement_id, restaurant_id, event_type, title, to_status, actor_id, actor_type, metadata)
                    VALUES ($1, $2, 'created', 'Settlement batch created', 'pending', $3, 'system', $4::jsonb)
                """, batch_id, rid, actor_id, _json.dumps({"cycle": cycle, "eta": eta.isoformat()}))

            # Insert transaction line
            tx_id = await conn.fetchval("""
                INSERT INTO bittu_settlement_transactions (
                    settlement_id, restaurant_id, branch_id,
                    payment_id, order_id,
                    gross_amount, fee_amount, gst_amount, net_amount,
                    razorpay_cut_amount,
                    transaction_type, payment_method, customer_name, order_reference,
                    settlement_status
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9,
                    $10,
                    'payment', $11, $12, $13,
                    'pending'
                )
                RETURNING id
            """,
                batch_id, rid, bid,
                pid, oid,
                float(gross), float(bittu_fee), float(gst_on_fee), float(net),
                float(razorpay_cut),
                payment_method, customer_name, order_reference,
            )

        logger.info(
            "settlement_enqueued",
            payment_id=payment_id,
            batch_id=str(batch_id),
            gross=float(gross),
            net=float(net),
        )

        await log_activity(
            user_id=actor_id,
            action="settlement.enqueued",
            entity_type="bittu_settlement_transaction",
            entity_id=str(tx_id),
            metadata={
                "payment_id": payment_id,
                "batch_id": str(batch_id),
                "gross": float(gross),
                "net": float(net),
            },
            branch_id=branch_id,
        )

        return {
            "settlement_id":             str(batch_id),
            "settlement_transaction_id": str(tx_id),
            "gross_amount":  float(gross),
            "bittu_fee":     float(bittu_fee),
            "gst_on_fee":    float(gst_on_fee),
            "razorpay_cut":  float(razorpay_cut),
            "net_amount":    float(net),
            "status":        "queued",
            "eta":           eta.isoformat(),
        }

    # ════════════════════════════════════════════════════════════════════════
    # TRANSITION SETTLEMENT STATUS  (internal / admin)
    # ════════════════════════════════════════════════════════════════════════

    async def transition_settlement(
        self,
        settlement_id: str,
        restaurant_id: str,
        new_status: str,
        actor_id: str = "system",
        actor_type: str = "system",
        bank_reference: Optional[str] = None,
        failure_reason: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        """
        Advance a settlement through its lifecycle.

        Valid transitions:
          pending      → processing
          processing   → sent_to_bank
          sent_to_bank → settled | failed
          failed       → processing  (retry)
          settled      → reversed

        On settled: creates accounting journal entry + updates daily_closings.
        """
        VALID_TRANSITIONS: dict[str, list[str]] = {
            "pending":      ["processing"],
            "processing":   ["sent_to_bank", "failed"],
            "sent_to_bank": ["settled", "failed"],
            "failed":       ["processing"],  # retry
            "settled":      ["reversed"],
        }

        sid = UUID(settlement_id)
        rid = UUID(restaurant_id)

        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM bittu_settlements
                WHERE id = $1 AND restaurant_id = $2
                FOR UPDATE
            """, sid, rid)

            if not row:
                raise NotFoundError("Settlement", settlement_id)

            current = row["settlement_status"]
            allowed = VALID_TRANSITIONS.get(current, [])
            if new_status not in allowed:
                raise ValidationError(
                    f"Cannot transition settlement from '{current}' to '{new_status}'. "
                    f"Allowed: {allowed}"
                )

            # Build update fields
            update_fields: dict = {"settlement_status": new_status, "updated_at": "NOW()"}
            if new_status == "settled":
                update_fields["settled_at"] = datetime.now(timezone.utc)
            if new_status in ("failed",):
                update_fields["retry_count"] = (row["retry_count"] or 0) + 1
                update_fields["last_attempt_at"] = datetime.now(timezone.utc)
                if failure_reason:
                    update_fields["failure_reason"] = failure_reason
            if bank_reference:
                update_fields["bank_reference_number"] = bank_reference

            await conn.execute("""
                UPDATE bittu_settlements
                SET settlement_status     = $2,
                    settled_at            = CASE WHEN $2 = 'settled' THEN NOW() ELSE settled_at END,
                    retry_count           = CASE WHEN $2 = 'failed'
                                                 THEN COALESCE(retry_count, 0) + 1
                                                 ELSE retry_count END,
                    last_attempt_at       = CASE WHEN $2 IN ('failed','processing')
                                                 THEN NOW() ELSE last_attempt_at END,
                    failure_reason        = COALESCE($3, failure_reason),
                    bank_reference_number = COALESCE($4, bank_reference_number),
                    updated_at            = NOW()
                WHERE id = $1
            """, sid, new_status, failure_reason, bank_reference)

            # Mirror status to all linked transactions
            await conn.execute("""
                UPDATE bittu_settlement_transactions
                SET settlement_status = $2
                WHERE settlement_id = $1
            """, sid, new_status)

            # Timeline entry
            event_titles = {
                "processing":   "Settlement processing started",
                "sent_to_bank": "Bank transfer initiated",
                "settled":      "Amount settled to bank account",
                "failed":       "Settlement failed",
                "reversed":     "Settlement reversed",
            }
            await conn.execute("""
                INSERT INTO bittu_settlement_timeline
                    (settlement_id, restaurant_id, event_type, title,
                     from_status, to_status, actor_id, actor_type, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            """,
                sid, rid,
                new_status,
                event_titles.get(new_status, new_status),
                current, new_status,
                actor_id, actor_type,
                _json.dumps({
                    "bank_reference": bank_reference,
                    "failure_reason": failure_reason,
                    **(metadata or {}),
                }),
            )

        # ── On SETTLED: create accounting entry + update daily_closings ──
        if new_status == "settled":
            await self._record_settlement_accounting(row, actor_id)
            await self._update_daily_closing_for_settlement(row)
            # Mirror into the immutable merchant ledger (best-effort,
            # idempotent on settlement_id).  Runs OUTSIDE the SERIALIZABLE
            # transaction so a ledger error cannot rollback the status
            # change — ledger is a parallel record, not the source of truth.
            await post_settlement_settled(
                settlement_row=dict(row),
                actor_id=actor_id,
            )
            # Phase 2: release escrow holds for every payment in the batch.
            await release_holds_for_settlement(
                settlement_row=dict(row),
                actor_id=actor_id,
            )
        elif new_status == "reversed":
            await post_settlement_reversed(
                settlement_row=dict(row),
                actor_id=actor_id,
            )

        await log_activity(
            user_id=actor_id,
            action=f"settlement.{new_status}",
            entity_type="bittu_settlement",
            entity_id=settlement_id,
            metadata={
                "from_status":    current,
                "to_status":      new_status,
                "bank_reference": bank_reference,
                "gross":          float(row["gross_amount"]),
                "net":            float(row["net_settlement_amount"]),
            },
        )

        return {
            "settlement_id": settlement_id,
            "from_status":   current,
            "to_status":     new_status,
            "bank_reference_number": bank_reference,
        }

    # ════════════════════════════════════════════════════════════════════════
    # ACCOUNTING INTEGRATION (internal)
    # ════════════════════════════════════════════════════════════════════════

    async def _record_settlement_accounting(self, row: dict, actor_id: str) -> None:
        """
        On settlement success, create accounting journal entry:

          DR Bank                    (net_settlement_amount)
          DR Gateway Charges / BITTU_FEE  (bittu_fee_amount)
          DR Gateway Tax / GST            (gst_amount)
          CR PG Clearing             (gross_amount)

        Reuses existing GATEWAY_CHARGES / GATEWAY_TAX / PG_CLEARING accounts.
        """
        settlement_id  = str(row["id"])
        restaurant_id  = str(row["restaurant_id"])
        branch_id      = str(row["branch_id"]) if row.get("branch_id") else None
        ref_id         = f"bittu_stl_{row['settlement_reference']}"

        gross = float(row["gross_amount"])
        fee   = float(row["bittu_fee_amount"])
        gst   = float(row["gst_amount"])
        net   = float(row["net_settlement_amount"])

        lines = []
        if net > 0:
            lines.append({
                "account": "BANK",
                "debit": net, "credit": 0,
                "description": f"Bittu settlement — {row['settlement_reference']}",
            })
        if fee > 0:
            lines.append({
                "account": "GATEWAY_CHARGES",
                "debit": fee, "credit": 0,
                "description": f"Bittu platform fee — {row['settlement_reference']}",
            })
        if gst > 0:
            lines.append({
                "account": "GATEWAY_TAX",
                "debit": gst, "credit": 0,
                "description": f"GST on Bittu fee — {row['settlement_reference']}",
            })
        lines.append({
            "account": "PG_CLEARING",
            "debit": 0, "credit": gross,
            "description": f"Bittu settlement cleared — {row['settlement_reference']}",
        })

        try:
            journal_id = await accounting_engine.create_journal_entry(
                reference_type="settlement",
                reference_id=ref_id,
                restaurant_id=restaurant_id,
                branch_id=branch_id,
                description=f"Bittu settlement — {row['settlement_reference']}",
                created_by=actor_id,
                source_event="BITTU_SETTLEMENT_SETTLED",
                lines=lines,
            )
            # Back-link the journal entry to the settlement
            async with get_connection() as conn:
                await conn.execute(
                    "UPDATE bittu_settlements SET journal_entry_id = $1 WHERE id = $2",
                    UUID(journal_id), row["id"],
                )
        except Exception as exc:
            logger.error(
                "settlement_accounting_failed",
                settlement_id=str(row["id"]),
                error=str(exc),
            )

    async def _update_daily_closing_for_settlement(self, row: dict) -> None:
        """
        Reflect settled amount in today's daily_closings row if it exists.
        Non-fatal: daily closing rows may not exist yet.
        """
        rid   = row["restaurant_id"]
        bid   = row.get("branch_id")
        today = ist_today()
        net   = float(row["net_settlement_amount"])
        fee   = float(row["bittu_fee_amount"]) + float(row["gst_amount"])

        try:
            async with get_connection() as conn:
                await conn.execute("""
                    UPDATE daily_closings
                    SET total_settled_today = total_settled_today + $3,
                        total_bittu_fees    = total_bittu_fees    + $4,
                        updated_at          = NOW()
                    WHERE restaurant_id = $1
                      AND closing_date = $2
                      AND COALESCE(branch_id::text, '') = $5
                      AND status != 'closed'
                """,
                    rid, today, net, fee,
                    str(bid) if bid else "",
                )
        except Exception as exc:
            logger.warning(
                "daily_closing_settlement_update_failed",
                settlement_id=str(row["id"]),
                error=str(exc),
            )

    # ════════════════════════════════════════════════════════════════════════
    # FEE CALCULATOR UTILITY
    # ════════════════════════════════════════════════════════════════════════

    def calculate_fee(self, gross_amount: float) -> dict:
        """
        Public utility: return the full per-settlement fee breakdown for a
        given gross amount under the 5 % deduction model.

        Used by the frontend to show a real-time fee preview before the
        merchant confirms a transaction, and by settlement-create routes
        that need every component (Razorpay cut included) for accounting.
        """
        br = _calc_settlement_breakdown(gross_amount)
        return {
            "gross_amount":           float(br["gross"]),
            "total_deduction_rate":   "5.00%",
            "total_deduction":        float(br["total_deduction"]),
            "razorpay_cut_rate":      "1.1682%",
            "razorpay_cut":           float(br["razorpay_cut"]),
            "platform_pool":          float(br["platform_pool"]),
            "bittu_fee_rate":         "3.2473%",
            "bittu_fee":              float(br["bittu_fee"]),
            "gst_rate":               "18%",
            "gst_on_fee":             float(br["gst_on_fee"]),
            "net_settlement":         float(br["net_to_merchant"]),
            "formula": (
                f"₹{float(br['gross']):,.2f} × 5% = ₹{float(br['total_deduction']):,.2f} cut → "
                f"₹{float(br['razorpay_cut']):,.4f} Razorpay + "
                f"₹{float(br['bittu_fee']):,.4f} fee + "
                f"₹{float(br['gst_on_fee']):,.4f} GST; "
                f"net ₹{float(br['net_to_merchant']):,.2f}"
            ),
        }

    # ════════════════════════════════════════════════════════════════════════
    # REFRESH MATERIALIZED VIEW
    # ════════════════════════════════════════════════════════════════════════

    async def refresh_summary_view(self) -> None:
        """
        Refresh the settlement daily summary MV. Call after bulk transitions.
        Non-blocking; failures logged but not raised.
        """
        try:
            async with get_connection() as conn:
                await conn.execute(
                    "REFRESH MATERIALIZED VIEW CONCURRENTLY mv_settlement_daily_summary"
                )
        except Exception as exc:
            logger.warning("mv_settlement_refresh_failed", error=str(exc))


statement_service = StatementService()
