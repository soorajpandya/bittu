"""
Single source of truth for the "non-revenue" order/payment filter.

A non-revenue order is one that should NOT appear on the operator-facing
Orders list and must NOT contribute to revenue / order-count / AOV
aggregations. The set mirrors the frontend filter shipped in release
`c1dc17d` so the two surfaces stay consistent.

Apply it server-side wherever an aggregation or listing risks pulling
in a cancelled QR, an abandoned (expired) payment intent, a refund, or
an order still waiting for its first payment.

Usage
-----
    from app.core.order_status import (
        NON_REVENUE_ORDER_STATUSES,
        NON_REVENUE_PAYMENT_STATUSES,
        non_revenue_where_sql,
    )

    # In a query that JOINs orders → payments:
    extra = non_revenue_where_sql(orders_alias="o", payments_alias="p")
    sql = f"SELECT ... FROM orders o LEFT JOIN payments p ... WHERE {extra}"

    # In a query touching orders only:
    extra = non_revenue_where_sql(orders_alias="o")
"""
from __future__ import annotations

from typing import Optional

# ── Filter sets (lowercase comparison only; keep both UK/US spellings) ──

NON_REVENUE_ORDER_STATUSES: frozenset[str] = frozenset({
    "cancelled",
    "canceled",
    "failed",
    "expired",
    "refunded",
    "pending_payment",
    "awaiting_payment",
})

NON_REVENUE_PAYMENT_STATUSES: frozenset[str] = frozenset({
    "cancelled",
    "canceled",
    "failed",
    "expired",
    "refunded",
})

# Pre-rendered SQL list literals (single-quoted, comma-separated) so we
# can splice them into raw SQL without exposing asyncpg's placeholder
# numbering to callers — the values are a closed enum, not user input.
_ORDER_LIST_SQL = ",".join(f"'{s}'" for s in sorted(NON_REVENUE_ORDER_STATUSES))
_PAYMENT_LIST_SQL = ",".join(f"'{s}'" for s in sorted(NON_REVENUE_PAYMENT_STATUSES))


def non_revenue_where_sql(
    *,
    orders_alias: Optional[str] = "o",
    payments_alias: Optional[str] = None,
) -> str:
    """
    Build a SQL fragment that keeps ONLY revenue orders.

    - When `payments_alias` is given, also filters the latest joined payment
      (NULL is treated as revenue-safe — many revenue paths have no payment
      row yet, e.g. completed cash orders before the payments insert).
    - Returns a parenthesised expression safe to AND into a larger WHERE.
    """
    parts: list[str] = []
    if orders_alias:
        parts.append(
            f"LOWER({orders_alias}.status) NOT IN ({_ORDER_LIST_SQL})"
        )
    if payments_alias:
        parts.append(
            f"({payments_alias}.status IS NULL "
            f"OR LOWER({payments_alias}.status) NOT IN ({_PAYMENT_LIST_SQL}))"
        )
    if not parts:
        return "TRUE"
    return "(" + " AND ".join(parts) + ")"


def is_non_revenue_order_status(status: Optional[str]) -> bool:
    return bool(status) and status.lower() in NON_REVENUE_ORDER_STATUSES


def is_non_revenue_payment_status(status: Optional[str]) -> bool:
    return bool(status) and status.lower() in NON_REVENUE_PAYMENT_STATUSES
