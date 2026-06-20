"""OpenAI function-calling schemas for the Bittu AI metrics toolbox.

Each entry describes one function in ``metrics_toolbox.TOOLS`` so GPT can pick
the right reader and arguments. Kept deliberately small/typed — the model only
chooses a tool name + a couple of enum/int args; never raw SQL.
"""
from __future__ import annotations

_PERIOD_ENUM = [
    "today",
    "yesterday",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "last_7_days",
    "last_30_days",
]


def _period_prop(default: str = "this_week") -> dict:
    return {
        "type": "string",
        "enum": _PERIOD_ENUM,
        "description": f"Reporting period. Defaults to {default}.",
    }


def _tool(name: str, description: str, properties: dict | None = None, required: list | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


TOOL_SCHEMAS: list[dict] = [
    _tool(
        "get_sales_summary",
        "Total revenue, order count, average order value, tax, discount and "
        "order-source split for a period. Use for questions like 'how much did "
        "I make today' or 'sales this week'.",
        {"period": _period_prop("today")},
    ),
    _tool(
        "get_revenue_trend",
        "Daily revenue/order series for a period plus comparison vs the "
        "previous equal-length window (growth %). Use for 'is business growing', "
        "'compare this week to last week'.",
        {"period": _period_prop("this_week")},
    ),
    _tool(
        "get_peak_hours",
        "Orders and revenue grouped by hour of day (IST). Use for staffing and "
        "'when are we busiest'.",
        {"period": _period_prop("last_7_days")},
    ),
    _tool(
        "get_top_items",
        "Best-selling menu items ranked by units sold or revenue. Use for 'top "
        "dishes', 'best sellers'.",
        {
            "period": _period_prop("this_week"),
            "by": {
                "type": "string",
                "enum": ["units", "revenue"],
                "description": "Rank by units sold or revenue. Defaults to units.",
            },
            "limit": {"type": "integer", "description": "How many items (1-50). Default 10."},
        },
    ),
    _tool(
        "get_menu_performance",
        "Classify menu items into Stars / Volume Drivers / Hidden Gems / "
        "Underperformers (median splits on units and revenue). Use for menu "
        "engineering and 'which items should I promote or remove'.",
        {"period": _period_prop("this_month")},
    ),
    _tool(
        "get_customer_segments",
        "RFM-lite customer segmentation: VIP, Regular, New, Lost, Occasional, "
        "with counts and example customers. Use for 'who are my best customers'.",
    ),
    _tool(
        "get_inactive_customers",
        "Customers who haven't ordered in N days, ranked by lifetime spend. Use "
        "for win-back campaigns and 'who should I re-engage'.",
        {
            "days": {"type": "integer", "description": "Inactivity threshold in days. Default 14."},
            "limit": {"type": "integer", "description": "Max customers to return (1-100). Default 20."},
        },
    ),
    _tool(
        "get_inventory_alerts",
        "Ingredients at or below their minimum stock level (reorder list). Use "
        "for 'what's running low', 'what should I restock'.",
    ),
    _tool(
        "get_inventory_consumption",
        "Ingredient consumption/wastage value over a period from the inventory "
        "ledger. Use for 'where is my stock going', food-cost questions.",
        {"period": _period_prop("last_7_days")},
    ),
    _tool(
        "get_expense_summary",
        "Expenses grouped by category for a period. Use for 'what are my costs', "
        "'biggest expenses'.",
        {"period": _period_prop("this_month")},
    ),
    _tool(
        "get_profit_snapshot",
        "Approximate operating profit = revenue - expenses - ingredient "
        "consumption, with margin %. Use for 'am I profitable', 'profit this "
        "month'. This is an estimate, not an audited P&L.",
        {"period": _period_prop("this_month")},
    ),
    _tool(
        "get_staff_performance",
        "Per-staff/waiter order and revenue attribution for a period, IF the "
        "orders carry staff attribution. Returns supported=false otherwise.",
        {"period": _period_prop("this_month")},
    ),
]
