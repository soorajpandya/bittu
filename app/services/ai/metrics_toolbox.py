"""Bittu AI — metrics toolbox.

The typed "tools" the AI assistant can call. Each function is a thin,
tenant-scoped reader over data Bittu POS already stores (orders,
order_items, customers, ingredients, inventory_ledger, expenses). No raw
user input ever becomes SQL — the model only chooses a function name and a
small set of typed arguments; the SQL here is fixed and parameterised.

All readers go through ``get_connection()`` so Postgres RLS is stamped, and
scope by ``tenant_where_clause`` (user_id / branch_id) or ``restaurant_id``
exactly like the rest of the codebase. Numbers are returned as plain floats
so they are JSON-serialisable for the chat-completions tool result.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from app.core.auth import UserContext
from app.core.database import get_connection
from app.core.ist import IST, ist_today, ist_day_start_utc, ist_day_end_utc, ist_now
from app.core.logging import get_logger
from app.core.order_status import non_revenue_where_sql
from app.core.tenant import tenant_where_clause
from app.services.expense_service import expense_service
from app.services.inventory_service import InventoryService

logger = get_logger(__name__)

_CONSUMPTION_TYPES = ("consumption", "wastage", "adjustment_out", "transfer_out")


# ── helpers ─────────────────────────────────────────────────────────────────

def _f(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _period_to_dates(period: Optional[str]) -> tuple[date, date, str]:
    """Resolve a friendly period keyword to an inclusive (from, to) IST date
    range plus a human label. Defaults to the last 7 days."""
    t = ist_today()
    p = (period or "last_7_days").strip().lower()

    if p in ("today",):
        return t, t, "today"
    if p in ("yesterday",):
        d = t - timedelta(days=1)
        return d, d, "yesterday"
    if p in ("this_week", "week"):
        start = t - timedelta(days=t.weekday())  # Monday
        return start, t, "this week"
    if p in ("last_week",):
        this_mon = t - timedelta(days=t.weekday())
        start = this_mon - timedelta(days=7)
        end = this_mon - timedelta(days=1)
        return start, end, "last week"
    if p in ("this_month", "month"):
        start = t.replace(day=1)
        return start, t, "this month"
    if p in ("last_month",):
        first_this = t.replace(day=1)
        end = first_this - timedelta(days=1)
        start = end.replace(day=1)
        return start, end, "last month"
    if p in ("last_30_days", "30d", "month_rolling"):
        return t - timedelta(days=29), t, "last 30 days"
    # default
    return t - timedelta(days=6), t, "last 7 days"


def _utc_bounds(from_date: date, to_date: date) -> tuple[datetime, datetime]:
    return ist_day_start_utc(from_date), ist_day_end_utc(to_date)


def _orders_where(
    user: UserContext,
    from_ts: datetime,
    to_ts: datetime,
    alias: str = "o",
    revenue_only: bool = True,
) -> tuple[str, list]:
    """Build a tenant-scoped + date-bounded WHERE for the orders table."""
    clause, params = tenant_where_clause(user, alias)
    params.append(from_ts)
    i_from = len(params)
    params.append(to_ts)
    i_to = len(params)
    where = (
        f"{clause} AND {alias}.created_at >= ${i_from} "
        f"AND {alias}.created_at < ${i_to}"
    )
    if revenue_only:
        where += f" AND {non_revenue_where_sql(orders_alias=alias)}"
    return where, params


def _pct(cur: float, prev: float) -> float:
    if prev == 0:
        return 100.0 if cur > 0 else 0.0
    return round((cur - prev) / prev * 100, 2)


# ── tools ─────────────────────────────────────────────────────────────────

async def get_sales_summary(user: UserContext, period: str = "today", **_) -> dict:
    """Revenue, order count, AOV, tax, discount and order-source split."""
    fd, td, label = _period_to_dates(period)
    from_ts, to_ts = _utc_bounds(fd, td)
    where, params = _orders_where(user, from_ts, to_ts)

    async with get_connection() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT COUNT(*)                              AS orders,
                   COALESCE(SUM(o.total_amount), 0)      AS revenue,
                   COALESCE(AVG(o.total_amount), 0)      AS aov,
                   COALESCE(SUM(o.tax_amount), 0)        AS tax,
                   COALESCE(SUM(o.discount_amount), 0)   AS discount,
                   COALESCE(SUM(o.subtotal), 0)          AS subtotal
            FROM orders o
            WHERE {where}
            """,
            *params,
        )
        by_source = await conn.fetch(
            f"""
            SELECT COALESCE(o.source::text, 'unknown') AS source,
                   COUNT(*)                            AS orders,
                   COALESCE(SUM(o.total_amount), 0)    AS revenue
            FROM orders o
            WHERE {where}
            GROUP BY o.source
            ORDER BY revenue DESC
            """,
            *params,
        )

    return {
        "period": label,
        "from": str(fd),
        "to": str(td),
        "revenue": round(_f(row["revenue"]), 2),
        "orders": int(row["orders"] or 0),
        "average_order_value": round(_f(row["aov"]), 2),
        "tax": round(_f(row["tax"]), 2),
        "discount": round(_f(row["discount"]), 2),
        "subtotal": round(_f(row["subtotal"]), 2),
        "by_source": [
            {
                "source": r["source"],
                "orders": int(r["orders"] or 0),
                "revenue": round(_f(r["revenue"]), 2),
            }
            for r in by_source
        ],
    }


async def get_revenue_trend(user: UserContext, period: str = "this_week", **_) -> dict:
    """Daily revenue/orders series for the period plus a comparison against
    the immediately-preceding equal-length window."""
    fd, td, label = _period_to_dates(period)
    from_ts, to_ts = _utc_bounds(fd, td)
    where, params = _orders_where(user, from_ts, to_ts)

    span_days = (td - fd).days + 1
    prev_to = fd - timedelta(days=1)
    prev_from = prev_to - timedelta(days=span_days - 1)
    p_from_ts, p_to_ts = _utc_bounds(prev_from, prev_to)
    prev_where, prev_params = _orders_where(user, p_from_ts, p_to_ts)

    async with get_connection() as conn:
        daily = await conn.fetch(
            f"""
            SELECT (o.created_at AT TIME ZONE 'Asia/Kolkata')::date AS day,
                   COUNT(*)                          AS orders,
                   COALESCE(SUM(o.total_amount), 0)  AS revenue
            FROM orders o
            WHERE {where}
            GROUP BY day
            ORDER BY day
            """,
            *params,
        )
        cur = await conn.fetchrow(
            f"SELECT COUNT(*) AS orders, COALESCE(SUM(o.total_amount),0) AS revenue "
            f"FROM orders o WHERE {where}",
            *params,
        )
        prev = await conn.fetchrow(
            f"SELECT COUNT(*) AS orders, COALESCE(SUM(o.total_amount),0) AS revenue "
            f"FROM orders o WHERE {prev_where}",
            *prev_params,
        )

    cur_rev, prev_rev = _f(cur["revenue"]), _f(prev["revenue"])
    cur_ord, prev_ord = int(cur["orders"] or 0), int(prev["orders"] or 0)
    return {
        "period": label,
        "from": str(fd),
        "to": str(td),
        "daily": [
            {
                "date": str(r["day"]),
                "orders": int(r["orders"] or 0),
                "revenue": round(_f(r["revenue"]), 2),
            }
            for r in daily
        ],
        "current": {"revenue": round(cur_rev, 2), "orders": cur_ord},
        "previous": {"revenue": round(prev_rev, 2), "orders": prev_ord},
        "change": {
            "revenue_pct": _pct(cur_rev, prev_rev),
            "orders_pct": _pct(cur_ord, prev_ord),
        },
    }


async def get_peak_hours(user: UserContext, period: str = "last_7_days", **_) -> dict:
    """Orders and revenue grouped by hour-of-day (IST)."""
    fd, td, label = _period_to_dates(period)
    from_ts, to_ts = _utc_bounds(fd, td)
    where, params = _orders_where(user, from_ts, to_ts)

    async with get_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT EXTRACT(HOUR FROM (o.created_at AT TIME ZONE 'Asia/Kolkata'))::int AS hour,
                   COUNT(*)                          AS orders,
                   COALESCE(SUM(o.total_amount), 0)  AS revenue
            FROM orders o
            WHERE {where}
            GROUP BY hour
            ORDER BY hour
            """,
            *params,
        )
    hours = [
        {
            "hour": int(r["hour"]),
            "orders": int(r["orders"] or 0),
            "revenue": round(_f(r["revenue"]), 2),
        }
        for r in rows
    ]
    busiest = max(hours, key=lambda h: h["orders"], default=None)
    return {"period": label, "by_hour": hours, "busiest_hour": busiest}


async def get_top_items(
    user: UserContext, period: str = "this_week", by: str = "units", limit: int = 10, **_
) -> dict:
    """Best-selling items by units or revenue (from order_items)."""
    fd, td, label = _period_to_dates(period)
    from_ts, to_ts = _utc_bounds(fd, td)
    where, params = _orders_where(user, from_ts, to_ts)
    order_col = "revenue" if (by or "").lower() in ("revenue", "profit") else "qty"
    note = None
    if (by or "").lower() == "profit":
        note = "True per-item profit needs recipe costing; ranked by revenue instead."
    params.append(max(1, min(int(limit or 10), 50)))
    limit_idx = len(params)

    async with get_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT oi.item_name                       AS name,
                   SUM(oi.quantity)                   AS qty,
                   COALESCE(SUM(oi.total_price), 0)   AS revenue,
                   COUNT(DISTINCT o.id)               AS orders
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            WHERE {where}
            GROUP BY oi.item_name
            ORDER BY {order_col} DESC NULLS LAST
            LIMIT ${limit_idx}
            """,
            *params,
        )
    items = [
        {
            "name": r["name"],
            "units_sold": int(r["qty"] or 0),
            "revenue": round(_f(r["revenue"]), 2),
            "orders": int(r["orders"] or 0),
        }
        for r in rows
    ]
    out = {"period": label, "ranked_by": order_col, "items": items}
    if note:
        out["note"] = note
    return out


async def get_menu_performance(user: UserContext, period: str = "this_month", **_) -> dict:
    """Classify items into Stars / Volume Drivers / Hidden Gems /
    Underperformers using median splits on units sold and revenue."""
    fd, td, label = _period_to_dates(period)
    from_ts, to_ts = _utc_bounds(fd, td)
    where, params = _orders_where(user, from_ts, to_ts)

    async with get_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT oi.item_name                     AS name,
                   SUM(oi.quantity)                 AS qty,
                   COALESCE(SUM(oi.total_price), 0) AS revenue
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            WHERE {where}
            GROUP BY oi.item_name
            """,
            *params,
        )

    data = [
        {"name": r["name"], "units_sold": int(r["qty"] or 0), "revenue": round(_f(r["revenue"]), 2)}
        for r in rows
    ]
    if not data:
        return {"period": label, "items": [], "summary": {}, "note": "No sales in this period."}

    def _median(values: list[float]) -> float:
        s = sorted(values)
        n = len(s)
        if n == 0:
            return 0.0
        mid = n // 2
        return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2

    qty_med = _median([d["units_sold"] for d in data])
    rev_med = _median([d["revenue"] for d in data])

    counts = {"Star": 0, "Volume Driver": 0, "Hidden Gem": 0, "Underperformer": 0}
    for d in data:
        hi_qty = d["units_sold"] >= qty_med
        hi_rev = d["revenue"] >= rev_med
        if hi_qty and hi_rev:
            cat = "Star"
        elif hi_qty and not hi_rev:
            cat = "Volume Driver"
        elif not hi_qty and hi_rev:
            cat = "Hidden Gem"
        else:
            cat = "Underperformer"
        d["category"] = cat
        counts[cat] += 1

    data.sort(key=lambda d: d["revenue"], reverse=True)
    return {
        "period": label,
        "thresholds": {"median_units": qty_med, "median_revenue": rev_med},
        "summary": counts,
        "items": data[:40],
        "recommendation_hint": (
            "Promote Stars and Hidden Gems; bundle Volume Drivers; review or "
            "remove persistent Underperformers."
        ),
    }


async def _customer_aggregates(user: UserContext) -> list[dict]:
    """Per-customer lifetime aggregates over revenue orders."""
    clause, params = tenant_where_clause(user, "o")
    where = f"{clause} AND o.customer_id IS NOT NULL AND {non_revenue_where_sql(orders_alias='o')}"
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT o.customer_id,
                   c.name,
                   c.phone_number,
                   COUNT(*)                          AS orders,
                   COALESCE(SUM(o.total_amount), 0)  AS spend,
                   MAX(o.created_at)                 AS last_order,
                   MIN(o.created_at)                 AS first_order
            FROM orders o
            JOIN customers c ON c.id = o.customer_id
            WHERE {where}
            GROUP BY o.customer_id, c.name, c.phone_number
            """,
            *params,
        )
    return [dict(r) for r in rows]


def _days_since(ts: Optional[datetime]) -> Optional[int]:
    if ts is None:
        return None
    if ts.tzinfo is None:
        from datetime import timezone as _tz
        ts = ts.replace(tzinfo=_tz.utc)
    return (ist_now() - ts.astimezone(IST)).days


async def get_customer_segments(user: UserContext, **_) -> dict:
    """RFM-lite segmentation: VIP / Regular / New / Lost / Occasional."""
    rows = await _customer_aggregates(user)
    if not rows:
        return {"total_customers": 0, "segments": {}, "note": "No customer order history yet."}

    spends = sorted(_f(r["spend"]) for r in rows)
    p90 = spends[int(len(spends) * 0.9)] if len(spends) > 1 else spends[-1]

    segments: dict[str, list[dict]] = {
        "VIP": [], "Regular": [], "New": [], "Lost": [], "Occasional": []
    }
    for r in rows:
        spend = _f(r["spend"])
        orders = int(r["orders"] or 0)
        recency = _days_since(r["last_order"])
        first_recency = _days_since(r["first_order"])
        entry = {
            "name": r["name"],
            "phone": r["phone_number"],
            "orders": orders,
            "spend": round(spend, 2),
            "days_since_last_order": recency,
        }
        if recency is not None and recency > 45:
            seg = "Lost"
        elif orders <= 1 and (first_recency is not None and first_recency <= 30):
            seg = "New"
        elif spend >= p90 and orders >= 3:
            seg = "VIP"
        elif orders >= 3:
            seg = "Regular"
        else:
            seg = "Occasional"
        segments[seg].append(entry)

    summary = {k: len(v) for k, v in segments.items()}
    # top few examples per segment to keep the payload compact
    examples = {
        k: sorted(v, key=lambda e: e["spend"], reverse=True)[:5]
        for k, v in segments.items()
    }
    return {
        "total_customers": len(rows),
        "high_value_threshold": round(p90, 2),
        "segments": summary,
        "examples": examples,
        "rules": (
            "Lost: no order in 45+ days. New: 1 order, first within 30 days. "
            "VIP: top-10% spender with 3+ orders. Regular: 3+ orders. "
            "Occasional: everyone else."
        ),
    }


async def get_inactive_customers(user: UserContext, days: int = 14, limit: int = 20, **_) -> dict:
    """Customers who have not ordered in `days`, ranked by lifetime spend."""
    days = max(1, int(days or 14))
    rows = await _customer_aggregates(user)
    inactive = []
    for r in rows:
        recency = _days_since(r["last_order"])
        if recency is not None and recency >= days:
            inactive.append(
                {
                    "name": r["name"],
                    "phone": r["phone_number"],
                    "orders": int(r["orders"] or 0),
                    "spend": round(_f(r["spend"]), 2),
                    "days_since_last_order": recency,
                    "last_order": str(r["last_order"]),
                }
            )
    inactive.sort(key=lambda e: e["spend"], reverse=True)
    return {
        "inactive_days_threshold": days,
        "count": len(inactive),
        "customers": inactive[: max(1, min(int(limit or 20), 100))],
    }


async def get_inventory_alerts(user: UserContext, **_) -> dict:
    """Ingredients at or below their minimum stock level."""
    rows = await InventoryService().get_stock_levels(user, low_stock_only=True)
    items = [
        {
            "name": r.get("name"),
            "unit": r.get("unit"),
            "current_stock": _f(r.get("current_stock")),
            "minimum_stock": _f(r.get("minimum_stock")),
            "supplier": r.get("supplier"),
        }
        for r in rows
    ]
    return {"low_stock_count": len(items), "items": items}


async def get_inventory_consumption(user: UserContext, period: str = "last_7_days", **_) -> dict:
    """Ingredient consumption/wastage value over the period (inventory_ledger)."""
    if not user.restaurant_id:
        return {"period": period, "items": [], "note": "No restaurant context."}
    fd, td, label = _period_to_dates(period)
    from_ts, to_ts = _utc_bounds(fd, td)
    types = ",".join(f"'{t}'" for t in _CONSUMPTION_TYPES)
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT i.name, i.unit,
                   SUM(l.quantity_out)                              AS qty,
                   SUM(l.quantity_out * COALESCE(l.unit_cost, 0))   AS value
            FROM inventory_ledger l
            JOIN ingredients i ON i.id = l.ingredient_id
            WHERE l.restaurant_id = $1::uuid
              AND l.created_at >= $2 AND l.created_at < $3
              AND l.transaction_type IN ({types})
            GROUP BY i.name, i.unit
            ORDER BY value DESC NULLS LAST
            LIMIT 25
            """,
            user.restaurant_id, from_ts, to_ts,
        )
    items = [
        {
            "name": r["name"],
            "unit": r["unit"],
            "quantity_consumed": round(_f(r["qty"]), 3),
            "value_consumed": round(_f(r["value"]), 2),
        }
        for r in rows
    ]
    total = round(sum(i["value_consumed"] for i in items), 2)
    return {"period": label, "total_value_consumed": total, "items": items}


async def get_expense_summary(user: UserContext, period: str = "this_month", **_) -> dict:
    """Expenses by category for the period."""
    if not user.restaurant_id:
        return {"period": period, "total": 0.0, "by_category": [], "note": "No restaurant context."}
    fd, td, label = _period_to_dates(period)
    data = await expense_service.expense_summary(user.restaurant_id, fd, td)
    data["period"] = label
    by_cat = data.get("by_category") or []
    data["by_category"] = [
        {"category": c.get("category"), "count": int(c.get("count") or 0), "total": _f(c.get("total"))}
        for c in by_cat
    ]
    return data


async def get_profit_snapshot(user: UserContext, period: str = "this_month", **_) -> dict:
    """Best-effort operating profit: revenue - expenses - inventory consumed.

    This is an approximation from available data, NOT an audited P&L.
    """
    sales = await get_sales_summary(user, period=period)
    revenue = sales["revenue"]
    expenses = (await get_expense_summary(user, period=period)).get("total", 0.0)
    consumption = (await get_inventory_consumption(user, period=period)).get(
        "total_value_consumed", 0.0
    )
    operating_profit = round(revenue - expenses - consumption, 2)
    margin = round((operating_profit / revenue * 100), 2) if revenue else 0.0
    return {
        "period": sales["period"],
        "revenue": revenue,
        "expenses": round(_f(expenses), 2),
        "inventory_consumed_value": round(_f(consumption), 2),
        "estimated_operating_profit": operating_profit,
        "estimated_margin_pct": margin,
        "note": "Approximation from sales, expenses and ingredient consumption; not an audited P&L.",
    }


async def get_staff_performance(user: UserContext, period: str = "this_month", **_) -> dict:
    """Per-staff order/revenue attribution, IF orders carry a staff column.

    Returns supported=false gracefully when no attribution column exists, so
    the assistant can answer honestly instead of inventing numbers.
    """
    fd, td, label = _period_to_dates(period)
    from_ts, to_ts = _utc_bounds(fd, td)
    candidates = ("created_by", "staff_id", "waiter_id", "served_by", "cashier_id")
    async with get_connection() as conn:
        col = await conn.fetchval(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'orders' AND column_name = ANY($1::text[])
            ORDER BY array_position($1::text[], column_name)
            LIMIT 1
            """,
            list(candidates),
        )
        if not col:
            return {
                "supported": False,
                "message": "Staff/waiter attribution is not tracked on orders yet, so per-staff performance cannot be computed.",
            }
        where, params = _orders_where(user, from_ts, to_ts)
        rows = await conn.fetch(
            f"""
            SELECT o.{col}::text                       AS staff,
                   COUNT(*)                            AS orders,
                   COALESCE(SUM(o.total_amount), 0)    AS revenue
            FROM orders o
            WHERE {where} AND o.{col} IS NOT NULL
            GROUP BY o.{col}
            ORDER BY revenue DESC
            LIMIT 25
            """,
            *params,
        )
    return {
        "supported": True,
        "period": label,
        "attribution_column": col,
        "staff": [
            {
                "staff": r["staff"],
                "orders": int(r["orders"] or 0),
                "revenue": round(_f(r["revenue"]), 2),
            }
            for r in rows
        ],
    }


# ── dispatch registry ────────────────────────────────────────────────────────

TOOLS = {
    "get_sales_summary": get_sales_summary,
    "get_revenue_trend": get_revenue_trend,
    "get_peak_hours": get_peak_hours,
    "get_top_items": get_top_items,
    "get_menu_performance": get_menu_performance,
    "get_customer_segments": get_customer_segments,
    "get_inactive_customers": get_inactive_customers,
    "get_inventory_alerts": get_inventory_alerts,
    "get_inventory_consumption": get_inventory_consumption,
    "get_expense_summary": get_expense_summary,
    "get_profit_snapshot": get_profit_snapshot,
    "get_staff_performance": get_staff_performance,
}


async def run_tool(name: str, user: UserContext, arguments: dict[str, Any]) -> dict:
    """Execute a toolbox function by name with model-supplied arguments."""
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return await fn(user, **(arguments or {}))
    except Exception as exc:  # noqa: BLE001 — surface a clean error to the model
        logger.warning("bittu_ai_tool_failed", tool=name, error=str(exc))
        return {"error": f"tool '{name}' failed: {exc}"}
