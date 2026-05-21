"""
Platform overview KPIs for the Burptech super-admin cockpit.

All numbers are computed live (no cache). Endpoints are expected to be
hit by a single internal dashboard, not by high-traffic clients.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.database import get_service_connection


async def platform_overview() -> dict[str, Any]:
    """Single payload for the super-admin landing page."""
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    yesterday_start = today_start - timedelta(days=1)
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    one_day_ago = datetime.now(timezone.utc) - timedelta(days=1)

    async with get_service_connection() as conn:
        # Merchants
        merchants = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                            AS total,
                COUNT(*) FILTER (WHERE suspended_at IS NOT NULL)    AS suspended,
                COUNT(*) FILTER (WHERE created_at >= $1)            AS new_today
              FROM restaurants
            """,
            today_start,
        )

        # KYC funnel
        kyc = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'draft')         AS draft,
                COUNT(*) FILTER (WHERE status = 'submitted')     AS submitted,
                COUNT(*) FILTER (WHERE status = 'under_review')  AS under_review,
                COUNT(*) FILTER (WHERE status = 'approved')      AS approved,
                COUNT(*) FILTER (WHERE status = 'rejected')      AS rejected,
                COUNT(*) FILTER (WHERE status = 'suspended')     AS suspended
              FROM merchant_kyc_profiles
            """,
        )

        # Route activation funnel
        route = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total_linked_accounts,
                COUNT(*) FILTER (WHERE stakeholder_id IS NOT NULL)              AS with_stakeholder,
                COUNT(*) FILTER (WHERE route_product_id IS NOT NULL)            AS product_requested,
                COUNT(*) FILTER (WHERE route_product_status = 'activated')      AS activated,
                COUNT(*) FILTER (WHERE bank_account_last4 IS NOT NULL)          AS with_bank
              FROM rzp_route_accounts
            """,
        )

        # Today's GMV (sum of captured payments)
        gmv_today = await conn.fetchval(
            """
            SELECT COALESCE(SUM(amount_paise), 0)
              FROM rzp_payments
             WHERE status = 'captured' AND captured_at >= $1
            """,
            today_start,
        ) or 0
        gmv_yday = await conn.fetchval(
            """
            SELECT COALESCE(SUM(amount_paise), 0)
              FROM rzp_payments
             WHERE status = 'captured'
               AND captured_at >= $1 AND captured_at < $2
            """,
            yesterday_start, today_start,
        ) or 0

        # Today's transfers (Route)
        transfers_today = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                AS count,
                COALESCE(SUM(amount_paise), 0)          AS amount_paise
              FROM rzp_route_transfers
             WHERE created_at >= $1
            """,
            today_start,
        )

        # Webhook health (last 1h)
        webhooks_1h = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                                              AS total,
                COUNT(*) FILTER (WHERE processing_state = 'processed')                AS processed,
                COUNT(*) FILTER (WHERE processing_state = 'failed')                   AS failed,
                COUNT(*) FILTER (WHERE processing_state IN ('received','processing')
                                 AND received_at < now() - interval '5 minutes')      AS stuck
              FROM payment_webhook_events
             WHERE received_at >= $1
            """,
            one_hour_ago,
        )

        # Open disputes
        disputes = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                            AS total_open,
                COUNT(*) FILTER (WHERE status = 'under_review')     AS under_review,
                COALESCE(SUM(amount_paise) FILTER (
                    WHERE status NOT IN ('won','lost','closed')
                ), 0)                                               AS exposure_paise
              FROM rzp_disputes
            """,
        )

        # Settlements last 24h
        settlements_24h = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                AS count,
                COALESCE(SUM(amount_paise), 0)          AS amount_paise
              FROM rzp_settlements
             WHERE created_at >= $1
            """,
            one_day_ago,
        )

    return {
        "as_of": datetime.now(timezone.utc),
        "merchants": dict(merchants) if merchants else {},
        "kyc_funnel": dict(kyc) if kyc else {},
        "route_funnel": dict(route) if route else {},
        "gmv": {
            "today_paise": int(gmv_today),
            "yesterday_paise": int(gmv_yday),
        },
        "transfers_today": dict(transfers_today) if transfers_today else {},
        "webhooks_last_1h": dict(webhooks_1h) if webhooks_1h else {},
        "disputes": dict(disputes) if disputes else {},
        "settlements_last_24h": dict(settlements_24h) if settlements_24h else {},
    }
