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

import json
from typing import Any, Optional

from app.core.database import get_service_connection
from app.services.razorpay.route_service import rzp_route_service

# Valid values for rzp_route_accounts.status (rzp_route_account_state enum).
_VALID_ACCOUNT_STATES = {"created", "activated", "suspended", "rejected", "deleted"}


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
            SELECT t.transfer_id,
                   t.razorpay_payment_id,
                   t.amount_paise,
                   t.status::text       AS status,
                   t.on_hold,
                   t.on_hold_until,
                   t.created_at,
                   t.processed_at,
                   t.reversed_at,
                   t.recipient_settlement_id,
                   t.refund_id,
                   t.reversal_of_transfer_id,
                   t.razorpay_order_id,
                   t.internal_order_id::text AS internal_order_id,
                   s.status::text       AS settlement_status,
                   s.settled_at         AS settled_at,
                   s.utr                AS settlement_utr
              FROM rzp_route_transfers t
              LEFT JOIN rzp_settlements s
                     ON s.settlement_id = t.recipient_settlement_id
             WHERE t.merchant_id = $1::uuid
             ORDER BY t.created_at DESC
             LIMIT 25
            """,
            merchant_id,
        )
        settlement_summary = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'failed')   AS settlements_failed,
                COUNT(*) FILTER (WHERE status = 'pending')  AS settlements_pending,
                COUNT(*) FILTER (WHERE status = 'processed') AS settlements_processed,
                COALESCE(MAX(settled_at), NULL)             AS last_settled_at
              FROM rzp_settlements
             WHERE merchant_id = $1::uuid
            """,
            merchant_id,
        )
    out = dict(row)
    out["recent_transfers"] = [dict(t) for t in recent_transfers]
    out["settlement_summary"] = dict(settlement_summary) if settlement_summary else {}
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


# ─────────────────────── backfill / repair ────────────────────────────
# Surgical data-fix endpoints that let ops seed or correct the
# rzp_route_accounts row WITHOUT calling Razorpay. These replace the
# ad-hoc gitignored `_backfill_*.py` / `_inspect_route_account.py`
# scripts that previously had to be shipped to EC2 by hand.

async def _merchant_exists(conn, merchant_id: str) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM restaurants WHERE id = $1::uuid", merchant_id
        )
    )


async def backfill_linked_account(
    *,
    merchant_id: str,
    linked_account_id: str,
    status: str = "activated",
    kyc_status: Optional[str] = "activated",
    activation_status: Optional[str] = "activated",
    route_product_status: Optional[str] = "activated",
    route_product_id: Optional[str] = None,
    stakeholder_id: Optional[str] = None,
    legal_business_name: Optional[str] = None,
    business_type: Optional[str] = None,
    contact_name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    reference_id: Optional[str] = None,
    bank_account_ifsc: Optional[str] = None,
    bank_account_last4: Optional[str] = None,
    tnc_accepted: bool = True,
    notes: Optional[dict] = None,
) -> dict[str, Any]:
    """Upsert a rzp_route_accounts row for a merchant (idempotent).

    merchant_id is UNIQUE, so re-running updates the existing row.
    notes are MERGED (existing || provided) and always stamped with
    ``bittu_merchant_id`` so downstream reconcilers can map back.
    """
    if status not in _VALID_ACCOUNT_STATES:
        raise ValueError(
            f"invalid status {status!r}; allowed: {sorted(_VALID_ACCOUNT_STATES)}"
        )

    merged_notes = dict(notes or {})
    merged_notes["bittu_merchant_id"] = merchant_id
    notes_json = json.dumps(merged_notes)

    async with get_service_connection() as conn:
        if not await _merchant_exists(conn, merchant_id):
            raise LookupError("merchant (restaurant) not found")

        # Guard: linked_account_id is UNIQUE — refuse to steal another
        # merchant's account here. Use /repoint for an intentional move.
        owner = await conn.fetchval(
            "SELECT merchant_id::text FROM rzp_route_accounts "
            "WHERE linked_account_id = $1",
            linked_account_id,
        )
        if owner and owner != merchant_id:
            raise ValueError(
                f"linked_account_id {linked_account_id} already belongs to "
                f"merchant {owner}; use the repoint endpoint to move it"
            )

        row = await conn.fetchrow(
            """
            INSERT INTO rzp_route_accounts (
                merchant_id, linked_account_id, status, kyc_status,
                activation_status, route_product_status, route_product_id,
                stakeholder_id, legal_business_name, business_type,
                contact_name, email, phone, reference_id,
                bank_account_ifsc, bank_account_last4,
                tnc_accepted_at, notes,
                route_product_activated_at
            ) VALUES (
                $1::uuid, $2, $3::rzp_route_account_state, $4,
                $5, $6, $7,
                $8, $9, $10,
                $11, $12, $13, $14,
                $15, $16,
                CASE WHEN $17 THEN NOW() ELSE NULL END, $18::jsonb,
                CASE WHEN $6 = 'activated' THEN NOW() ELSE NULL END
            )
            ON CONFLICT (merchant_id) DO UPDATE SET
                linked_account_id    = EXCLUDED.linked_account_id,
                status               = EXCLUDED.status,
                kyc_status           = COALESCE(EXCLUDED.kyc_status, rzp_route_accounts.kyc_status),
                activation_status    = COALESCE(EXCLUDED.activation_status, rzp_route_accounts.activation_status),
                route_product_status = COALESCE(EXCLUDED.route_product_status, rzp_route_accounts.route_product_status),
                route_product_id     = COALESCE(EXCLUDED.route_product_id, rzp_route_accounts.route_product_id),
                stakeholder_id       = COALESCE(EXCLUDED.stakeholder_id, rzp_route_accounts.stakeholder_id),
                legal_business_name  = COALESCE(EXCLUDED.legal_business_name, rzp_route_accounts.legal_business_name),
                business_type        = COALESCE(EXCLUDED.business_type, rzp_route_accounts.business_type),
                contact_name         = COALESCE(EXCLUDED.contact_name, rzp_route_accounts.contact_name),
                email                = COALESCE(EXCLUDED.email, rzp_route_accounts.email),
                phone                = COALESCE(EXCLUDED.phone, rzp_route_accounts.phone),
                reference_id         = COALESCE(EXCLUDED.reference_id, rzp_route_accounts.reference_id),
                bank_account_ifsc    = COALESCE(EXCLUDED.bank_account_ifsc, rzp_route_accounts.bank_account_ifsc),
                bank_account_last4   = COALESCE(EXCLUDED.bank_account_last4, rzp_route_accounts.bank_account_last4),
                tnc_accepted_at      = COALESCE(rzp_route_accounts.tnc_accepted_at, EXCLUDED.tnc_accepted_at),
                route_product_activated_at = COALESCE(rzp_route_accounts.route_product_activated_at, EXCLUDED.route_product_activated_at),
                notes                = rzp_route_accounts.notes || EXCLUDED.notes,
                updated_at           = NOW()
            RETURNING merchant_id::text AS merchant_id, linked_account_id,
                      status::text AS status, kyc_status, activation_status,
                      route_product_status, route_product_id, stakeholder_id,
                      notes, created_at, updated_at
            """,
            merchant_id, linked_account_id, status, kyc_status,
            activation_status, route_product_status, route_product_id,
            stakeholder_id, legal_business_name, business_type,
            contact_name, email, phone, reference_id,
            bank_account_ifsc, bank_account_last4,
            tnc_accepted, notes_json,
        )
    return {"ok": True, "account": dict(row)}


async def repoint_linked_account(
    *,
    merchant_id: str,
    linked_account_id: str,
    notes: Optional[dict] = None,
) -> dict[str, Any]:
    """Move an existing linked account (acc_xxx) to a different merchant.

    merchant_id is UNIQUE, so the target merchant must NOT already own a
    route account. The row's notes are stamped with the new
    ``bittu_merchant_id`` and a ``repointed_from`` audit trail.
    """
    async with get_service_connection() as conn:
        if not await _merchant_exists(conn, merchant_id):
            raise LookupError("target merchant (restaurant) not found")

        src = await conn.fetchrow(
            "SELECT merchant_id::text AS merchant_id, notes "
            "FROM rzp_route_accounts WHERE linked_account_id = $1",
            linked_account_id,
        )
        if not src:
            raise LookupError("linked_account_id not found")
        if src["merchant_id"] == merchant_id:
            raise ValueError("linked account already points to this merchant")

        existing_target = await conn.fetchval(
            "SELECT linked_account_id FROM rzp_route_accounts "
            "WHERE merchant_id = $1::uuid",
            merchant_id,
        )
        if existing_target:
            raise ValueError(
                f"target merchant already owns linked account "
                f"{existing_target}; remove it before repointing"
            )

        merged_notes = dict(notes or {})
        merged_notes["bittu_merchant_id"] = merchant_id
        merged_notes["repointed_from"] = src["merchant_id"]
        notes_json = json.dumps(merged_notes)

        row = await conn.fetchrow(
            """
            UPDATE rzp_route_accounts
               SET merchant_id = $1::uuid,
                   notes       = notes || $2::jsonb,
                   updated_at  = NOW()
             WHERE linked_account_id = $3
            RETURNING merchant_id::text AS merchant_id, linked_account_id,
                      status::text AS status, kyc_status, activation_status,
                      route_product_status, notes, updated_at
            """,
            merchant_id, notes_json, linked_account_id,
        )
    return {"ok": True, "repointed_from": src["merchant_id"], "account": dict(row)}

