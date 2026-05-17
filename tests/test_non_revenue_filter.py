"""
Lock the non-revenue order/payment status filter.

We don't have integration fixtures for real DB seeding in this repo, so
these tests guard the contract: the canonical set values, the SQL
fragment shape, and the fact that the orders service / analytics
service / reporting service actually splice them in.
"""
from __future__ import annotations

from app.core.order_status import (
    NON_REVENUE_ORDER_STATUSES,
    NON_REVENUE_PAYMENT_STATUSES,
    is_non_revenue_order_status,
    is_non_revenue_payment_status,
    non_revenue_where_sql,
)


def test_non_revenue_order_set_matches_frontend_contract():
    # Lock the exact frontend-visible set. Adding to this requires a
    # coordinated FE release — do not bump silently.
    assert NON_REVENUE_ORDER_STATUSES == frozenset({
        "cancelled", "canceled", "failed", "expired", "refunded",
        "pending_payment", "awaiting_payment",
    })


def test_non_revenue_payment_set_matches_frontend_contract():
    assert NON_REVENUE_PAYMENT_STATUSES == frozenset({
        "cancelled", "canceled", "failed", "expired", "refunded",
    })


def test_pending_sync_is_revenue():
    # `pending_sync` = offline cash awaiting upload; counts as revenue.
    assert not is_non_revenue_order_status("pending_sync")
    assert not is_non_revenue_order_status("completed")
    assert not is_non_revenue_order_status("paid")
    assert not is_non_revenue_order_status("captured")


def test_cancelled_classified_as_non_revenue():
    assert is_non_revenue_order_status("cancelled")
    assert is_non_revenue_order_status("CANCELLED")  # case-insensitive
    assert is_non_revenue_order_status("awaiting_payment")
    assert is_non_revenue_payment_status("expired")


def test_where_sql_excludes_orders_only():
    sql = non_revenue_where_sql(orders_alias="o")
    assert "LOWER(o.status) NOT IN" in sql
    assert "'cancelled'" in sql and "'expired'" in sql
    assert "'pending_payment'" in sql
    # payments alias not requested → no payments clause spliced
    assert ".status IS NULL" not in sql


def test_where_sql_handles_payments_alias():
    sql = non_revenue_where_sql(orders_alias="o", payments_alias="p")
    assert "LOWER(o.status) NOT IN" in sql
    assert "p.status IS NULL OR LOWER(p.status) NOT IN" in sql


def test_orders_service_splices_revenue_filter_by_default():
    # Smoke: the WHERE clause builder is wired into the service. Grep
    # the source to assert the filter is referenced (vs. lost in a rebase).
    import inspect
    from app.services import order_service

    src = inspect.getsource(order_service.OrderService.get_orders)
    assert "include_non_revenue" in src, (
        "get_orders must accept include_non_revenue kwarg"
    )
    assert "NON_REVENUE_ORDER_STATUSES" in src
    assert "NON_REVENUE_PAYMENT_STATUSES" in src
    assert "NOT EXISTS" in src, (
        "must exclude orders whose latest payment is non-revenue"
    )


def test_analytics_service_uses_revenue_filter_for_total_orders():
    import inspect
    from app.services import analytics_service

    src = inspect.getsource(analytics_service)
    # The fragment is defined at module top and FILTER-ed into the SELECT.
    assert "_REVENUE_FILTER_SQL" in src
    assert "FILTER (WHERE {_REVENUE_FILTER_SQL})  AS total_orders" in src


def test_reporting_service_pnl_filters_orders():
    import inspect
    from app.services import reporting_service

    src = inspect.getsource(reporting_service.ReportingService.pnl)
    assert "NON_REVENUE_ORDER_STATUSES" in src
    assert "LOWER(status) NOT IN" in src


def test_analytics_router_filters_dashboard_counts():
    import inspect
    from app.api.v1 import analytics as analytics_router

    src = inspect.getsource(analytics_router)
    assert "_REVENUE_ORDERS_FILTER" in src
    # Both endpoints (dashboard-counts + /daily fallback) must reference it.
    assert src.count("_REVENUE_ORDERS_FILTER") >= 3  # 1 const + 2 SELECTs
