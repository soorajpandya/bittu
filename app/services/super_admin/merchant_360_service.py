"""
Merchant 360° — one big read for a Burptech ops person triaging a
single merchant. Returns identity, KYC, Route, wallet, recent activity,
disputes, settlements and admin notes in a single shape.
"""
from __future__ import annotations

from typing import Any

from app.core.database import get_service_connection


async def merchant_360(restaurant_id: str) -> dict[str, Any]:
    async with get_service_connection() as conn:
        identity = await conn.fetchrow(
            """
            SELECT r.id::text             AS restaurant_id,
                   r.name,
                   r.owner_id::text       AS owner_id,
                   u.email                AS owner_email,
                   r.created_at,
                   r.updated_at,
                   r.suspended_at,
                   r.suspended_reason,
                   r.suspended_by::text   AS suspended_by,
                   sb.email               AS suspended_by_email
              FROM restaurants r
              LEFT JOIN auth.users u  ON u.id  = r.owner_id
              LEFT JOIN auth.users sb ON sb.id = r.suspended_by
             WHERE r.id = $1::uuid
            """,
            restaurant_id,
        )
        if not identity:
            raise LookupError("merchant not found")

        branches = await conn.fetch(
            """
            SELECT id::text AS id, name, is_main_branch, created_at
              FROM sub_branches
             WHERE restaurant_id = $1::uuid
             ORDER BY created_at
            """,
            restaurant_id,
        )

        kyc = await conn.fetchrow(
            """
            SELECT status::text                AS status,
                   business_type::text         AS business_type,
                   legal_name,
                   gstin, pan, cin,
                   risk_tier,
                   contact_email, contact_phone,
                   submitted_at, reviewed_at,
                   reviewed_by_admin_id::text  AS reviewed_by_admin_id,
                   approved_at, suspended_at,
                   rejection_reason, suspension_reason,
                   created_at, updated_at
              FROM merchant_kyc_profiles
             WHERE merchant_id = $1::uuid
            """,
            restaurant_id,
        )

        route = await conn.fetchrow(
            """
            SELECT linked_account_id,
                   legal_business_name,
                   business_type,
                   email,
                   phone,
                   status::text AS status,
                   kyc_status,
                   activation_status,
                   stakeholder_id,
                   route_product_id,
                   route_product_status,
                   route_product_requested_at,
                   route_product_activated_at,
                   bank_account_last4,
                   bank_account_ifsc,
                   tnc_accepted_at,
                   created_at,
                   updated_at
              FROM rzp_route_accounts
             WHERE merchant_id = $1::uuid
            """,
            restaurant_id,
        )

        wallet = await conn.fetchrow(
            """
            SELECT current_balance, currency, last_posted_at
              FROM merchant_ledger_balance_locks
             WHERE merchant_id = $1::uuid
            """,
            restaurant_id,
        )

        recent_payments = await conn.fetch(
            """
            SELECT razorpay_payment_id, status::text AS status, method,
                   amount_paise, fee_paise, currency, captured_at, created_at
              FROM rzp_payments
             WHERE merchant_id = $1::uuid
             ORDER BY created_at DESC
             LIMIT 20
            """,
            restaurant_id,
        )

        recent_settlements = await conn.fetch(
            """
            SELECT settlement_id, amount_paise, fees_paise, tax_paise,
                   utr, status::text AS status, settled_at, created_for_date,
                   created_at
              FROM rzp_settlements
             WHERE merchant_id = $1::uuid
             ORDER BY created_at DESC
             LIMIT 20
            """,
            restaurant_id,
        )

        open_disputes = await conn.fetch(
            """
            SELECT dispute_id, razorpay_payment_id, amount_paise, currency,
                   reason_code, phase, status::text AS status, deadline_at,
                   created_at
              FROM rzp_disputes
             WHERE merchant_id = $1::uuid
               AND status NOT IN ('won', 'lost', 'closed')
             ORDER BY created_at DESC
             LIMIT 25
            """,
            restaurant_id,
        )

        recent_transfers = await conn.fetch(
            """
            SELECT transfer_id, razorpay_payment_id, amount_paise,
                   status::text AS status, on_hold, on_hold_until,
                   created_at, processed_at, reversed_at
              FROM rzp_route_transfers
             WHERE merchant_id = $1::uuid
             ORDER BY created_at DESC
             LIMIT 20
            """,
            restaurant_id,
        )

        notes = await conn.fetch(
            """
            SELECT id::text AS id, note, author_id::text AS author_id,
                   author_email, created_at
              FROM merchant_admin_notes
             WHERE merchant_id = $1::uuid
             ORDER BY created_at DESC
             LIMIT 25
            """,
            restaurant_id,
        )

        kpis = await conn.fetchrow(
            """
            SELECT
                COALESCE((
                    SELECT SUM(amount_paise) FROM rzp_payments
                     WHERE merchant_id = $1::uuid AND status = 'captured'
                       AND captured_at >= now() - interval '30 days'
                ), 0)                                                AS gmv_30d_paise,
                COALESCE((
                    SELECT COUNT(*) FROM rzp_payments
                     WHERE merchant_id = $1::uuid AND status = 'captured'
                       AND captured_at >= now() - interval '30 days'
                ), 0)                                                AS payments_30d,
                COALESCE((
                    SELECT SUM(amount_paise) FROM rzp_disputes
                     WHERE merchant_id = $1::uuid
                       AND status NOT IN ('won','lost','closed')
                ), 0)                                                AS dispute_exposure_paise
            """,
            restaurant_id,
        )

    ident = dict(identity)
    ident["is_suspended"] = ident.get("suspended_at") is not None
    return {
        "identity":           ident,
        "branches":           [dict(b) for b in branches],
        "kyc":                dict(kyc) if kyc else None,
        "route":              dict(route) if route else None,
        "wallet":             dict(wallet) if wallet else None,
        "kpis_30d":           dict(kpis) if kpis else {},
        "recent_payments":    [dict(p) for p in recent_payments],
        "recent_settlements": [dict(s) for s in recent_settlements],
        "open_disputes":      [dict(d) for d in open_disputes],
        "recent_transfers":   [dict(t) for t in recent_transfers],
        "admin_notes":        [dict(n) for n in notes],
    }
