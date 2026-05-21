"""
Platform-side Razorpay Route oversight for Burptech super-admins.

This is the cross-merchant counterpart to the per-merchant
`/razorpay-route/*` endpoints. Super-admins can:

  • List/search linked accounts and onboarding state across all merchants.
  • Force-sync any merchant's linked account / product config.
  • Force-onboard a merchant (delegates to `rzp_route_service.onboard_route_merchant`).
  • Inspect transfers across all merchants.

Distinct from the polling scheduler (`rzp_route_polling`) which periodically
refreshes EVERY merchant. These endpoints are surgical.
"""
from __future__ import annotations

from typing import Any, Optional

from app.core.database import get_service_connection
from app.services.razorpay.route_service import rzp_route_service


async def list_linked_accounts(
    *,
    status: Optional[str] = None,
    kyc_status: Optional[str] = None,
    route_product_status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    where: list[str] = []
    args: list[Any] = []
    n = 0

    def _arg(v):
        nonlocal n
        n += 1
        args.append(v)
        return f"${n}"

    if status:
        where.append(f"r.status::text = {_arg(status)}")
    if kyc_status:
        where.append(f"r.kyc_status = {_arg(kyc_status)}")
    if route_product_status:
        where.append(f"r.route_product_status = {_arg(route_product_status)}")
    if search:
        like = f"%{search}%"
        where.append(
            f"(r.linked_account_id ILIKE {_arg(like)} "
            f"OR r.legal_business_name ILIKE {_arg(like)} "
            f"OR r.email ILIKE {_arg(like)} "
            f"OR rest.name ILIKE {_arg(like)})"
        )
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    args_with_pagination = [*args, limit, offset]
    limit_idx = n + 1
    offset_idx = n + 2

    async with get_service_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                r.merchant_id::text             AS merchant_id,
                rest.name                       AS merchant_name,
                r.linked_account_id,
                r.legal_business_name,
                r.business_type,
                r.email,
                r.phone,
                r.reference_id,
                r.status::text                  AS status,
                r.kyc_status,
                r.activation_status,
                r.stakeholder_id,
                r.route_product_id,
                r.route_product_status,
                r.route_product_requested_at,
                r.route_product_activated_at,
                r.tnc_accepted_at,
                r.bank_account_last4,
                r.bank_account_ifsc,
                r.created_at,
                r.updated_at
              FROM rzp_route_accounts r
              LEFT JOIN restaurants rest ON rest.id = r.merchant_id
              {where_sql}
             ORDER BY r.created_at DESC
             LIMIT ${limit_idx} OFFSET ${offset_idx}
            """,
            *args_with_pagination,
        )
        total = await conn.fetchval(
            f"""
            SELECT COUNT(*)
              FROM rzp_route_accounts r
              LEFT JOIN restaurants rest ON rest.id = r.merchant_id
              {where_sql}
            """,
            *args,
        )

    return {
        "total":  int(total or 0),
        "items":  [dict(r) for r in rows],
        "limit":  limit,
        "offset": offset,
    }


async def get_linked_account_full(merchant_id: str) -> dict[str, Any]:
    """Read-through to the per-merchant view but bypasses RLS."""
    async with get_service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                r.merchant_id::text         AS merchant_id,
                rest.name                   AS merchant_name,
                r.linked_account_id,
                r.legal_business_name,
                r.business_type,
                r.contact_name,
                r.email,
                r.phone,
                r.reference_id,
                r.status::text              AS status,
                r.kyc_status,
                r.activation_status,
                r.stakeholder_id,
                r.stakeholder_raw,
                r.route_product_id,
                r.route_product_status,
                r.route_product_requested_at,
                r.route_product_activated_at,
                r.route_product_raw,
                r.tnc_accepted_at,
                r.bank_account_last4,
                r.bank_account_ifsc,
                r.notes,
                r.created_at,
                r.updated_at
              FROM rzp_route_accounts r
              LEFT JOIN restaurants rest ON rest.id = r.merchant_id
             WHERE r.merchant_id = $1::uuid
            """,
            merchant_id,
        )
        if not row:
            raise LookupError("linked account not found for merchant")
        recent_transfers = await conn.fetch(
            """
            SELECT transfer_id, razorpay_payment_id, amount_paise,
                   status::text AS status, on_hold, on_hold_until,
                   created_at, processed_at, reversed_at
              FROM rzp_route_transfers
             WHERE merchant_id = $1::uuid
             ORDER BY created_at DESC
             LIMIT 25
            """,
            merchant_id,
        )
    out = dict(row)
    out["recent_transfers"] = [dict(t) for t in recent_transfers]
    return out


async def force_sync_linked_account(merchant_id: str) -> dict[str, Any]:
    return await rzp_route_service.sync_linked_account(merchant_id=merchant_id)


async def force_sync_product(merchant_id: str) -> dict[str, Any]:
    return await rzp_route_service.sync_route_product(merchant_id=merchant_id)


async def force_onboard(
    *,
    merchant_id: str,
    bank_account_number: str,
    ifsc: Optional[str] = None,
    beneficiary_name: Optional[str] = None,
    reference_id: Optional[str] = None,
    tnc_accepted: bool = True,
    extra_notes: Optional[dict] = None,
) -> dict[str, Any]:
    return await rzp_route_service.onboard_route_merchant(
        merchant_id=merchant_id,
        bank_account_number=bank_account_number,
        ifsc=ifsc,
        beneficiary_name=beneficiary_name,
        reference_id=reference_id,
        tnc_accepted=tnc_accepted,
        extra_notes=extra_notes,
    )


async def list_transfers(
    *,
    merchant_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    where: list[str] = []
    args: list[Any] = []
    n = 0

    def _arg(v):
        nonlocal n
        n += 1
        args.append(v)
        return f"${n}"

    if merchant_id:
        where.append(f"t.merchant_id = {_arg(merchant_id)}::uuid")
    if status:
        where.append(f"t.status::text = {_arg(status)}")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    args_with_pagination = [*args, limit, offset]
    limit_idx = n + 1
    offset_idx = n + 2

    async with get_service_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                t.transfer_id,
                t.razorpay_payment_id,
                t.recipient_account_id,
                t.merchant_id::text         AS merchant_id,
                rest.name                   AS merchant_name,
                t.amount_paise,
                t.fee_paise,
                t.tax_paise,
                t.status::text              AS status,
                t.on_hold,
                t.on_hold_until,
                t.processed_at,
                t.reversed_at,
                t.created_at
              FROM rzp_route_transfers t
              LEFT JOIN restaurants rest ON rest.id = t.merchant_id
              {where_sql}
             ORDER BY t.created_at DESC
             LIMIT ${limit_idx} OFFSET ${offset_idx}
            """,
            *args_with_pagination,
        )
        total = await conn.fetchval(
            f"""
            SELECT COUNT(*) FROM rzp_route_transfers t {where_sql}
            """,
            *args,
        )

    return {
        "total":  int(total or 0),
        "items":  [dict(r) for r in rows],
        "limit":  limit,
        "offset": offset,
    }


async def onboarding_queue(*, limit: int = 100) -> dict[str, Any]:
    """
    Onboarding board grouped by funnel state — what a human ops person
    needs to triage. Buckets:
      • no_linked_account        — restaurant exists but no rzp_route_accounts row
      • no_stakeholder           — linked account but no stakeholder yet
      • no_product               — stakeholder but no product configuration
      • awaiting_activation      — product requested but not activated
      • activated                — fully live
      • needs_clarification      — KYC blocked
      • rejected                 — Razorpay rejected
    """
    async with get_service_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT
                CASE
                  WHEN r.id IS NULL                                  THEN 'no_linked_account'
                  WHEN r.kyc_status = 'rejected'                     THEN 'rejected'
                  WHEN r.kyc_status = 'needs_clarification'          THEN 'needs_clarification'
                  WHEN r.stakeholder_id IS NULL                      THEN 'no_stakeholder'
                  WHEN r.route_product_id IS NULL                    THEN 'no_product'
                  WHEN r.route_product_status <> 'activated'
                       OR r.route_product_status IS NULL             THEN 'awaiting_activation'
                  ELSE 'activated'
                END                                                AS bucket,
                rest.id::text                                      AS merchant_id,
                rest.name                                          AS merchant_name,
                rest.created_at                                    AS merchant_created_at,
                r.linked_account_id,
                r.status::text                                     AS account_status,
                r.kyc_status,
                r.route_product_status,
                r.route_product_requested_at,
                r.route_product_activated_at
              FROM restaurants rest
              LEFT JOIN rzp_route_accounts r ON r.merchant_id = rest.id
             WHERE rest.suspended_at IS NULL
             ORDER BY rest.created_at DESC
             LIMIT $1
            """,
            limit,
        )

    buckets: dict[str, list[dict]] = {
        "no_linked_account": [], "no_stakeholder": [], "no_product": [],
        "awaiting_activation": [], "activated": [],
        "needs_clarification": [], "rejected": [],
    }
    for r in rows:
        d = dict(r)
        b = d.pop("bucket")
        buckets.setdefault(b, []).append(d)
    return {"counts": {k: len(v) for k, v in buckets.items()}, "buckets": buckets}
