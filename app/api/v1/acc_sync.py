"""Accounting Sync endpoints — bridge restaurant ↔ accounting.

Provides:
  GET  /accounting/sync/status          — dashboard: synced/unsynced counts
  POST /accounting/sync/bulk            — backfill unsynced paid orders
  POST /accounting/sync/order/{id}      — sync one order manually
  POST /accounting/sync/customer/{id}   — sync one customer to accounting contact
  POST /accounting/sync/daybook         — generate day book for a date
  GET  /accounting/sync/daybook/today   — get or generate today's day book
  GET  /accounting/sync/daybook         — list day books for date range
"""
from typing import Optional
from uuid import UUID
from datetime import date, timedelta
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_sync_service import (
    sync_payment_to_invoice,
    sync_customer_to_contact,
    bulk_sync_orders,
    get_sync_status,
    generate_day_book,
)
from app.core.database import get_connection

router = APIRouter(prefix="/accounting/sync", tags=["Accounting – Sync"])


_auth = require_permission("accounting:read")


# ──────────────────────────────────────────────────────────────
# Sync Status Dashboard
# ──────────────────────────────────────────────────────────────

@router.get("/status")
async def sync_status_dashboard(user: UserContext = Depends(_auth)):
    """Get overview of sync state between restaurant and accounting."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await get_sync_status(
        user_id=uid,
        branch_id=user.branch_id,
    )


# ──────────────────────────────────────────────────────────────
# Bulk Sync
# ──────────────────────────────────────────────────────────────

class BulkSyncRequest(BaseModel):
    from_date: Optional[date] = None
    to_date: Optional[date] = None


@router.post("/bulk")
async def bulk_sync(
    body: BulkSyncRequest = BulkSyncRequest(),
    user: UserContext = Depends(_auth),
):
    """Sync all paid orders that haven't been converted to accounting invoices yet."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await bulk_sync_orders(
        user_id=uid,
        branch_id=user.branch_id,
        from_date=body.from_date,
        to_date=body.to_date,
    )


# ──────────────────────────────────────────────────────────────
# Single Order Sync
# ──────────────────────────────────────────────────────────────

@router.post("/order/{order_id}")
async def sync_single_order(
    order_id: str,
    user: UserContext = Depends(_auth),
):
    """Manually sync a specific order to an accounting invoice."""
    uid = user.owner_id if user.is_branch_user else user.user_id

    # Find the payment for this order
    async with get_connection() as conn:
        payment = await conn.fetchrow(
            "SELECT id, amount FROM payments WHERE order_id = $1 AND status = 'completed' LIMIT 1",
            order_id,
        )
    if not payment:
        return {"error": "No completed payment found for this order"}

    return await sync_payment_to_invoice(
        order_id=order_id,
        payment_id=str(payment["id"]),
        amount=float(payment["amount"]),
        user_id=uid,
        branch_id=user.branch_id,
        restaurant_id=user.restaurant_id,
    )


# ──────────────────────────────────────────────────────────────
# Customer Sync
# ──────────────────────────────────────────────────────────────

@router.post("/customer/{customer_id}")
async def sync_single_customer(
    customer_id: int,
    user: UserContext = Depends(_auth),
):
    """Sync a restaurant customer to an accounting contact."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await sync_customer_to_contact(
        customer_id=customer_id,
        user_id=uid,
        branch_id=user.branch_id,
    )


# ──────────────────────────────────────────────────────────────
# Bulk Customer Sync
# ──────────────────────────────────────────────────────────────

@router.post("/customers")
async def sync_all_customers(user: UserContext = Depends(_auth)):
    """Sync all restaurant customers to accounting contacts."""
    uid = user.owner_id if user.is_branch_user else user.user_id

    async with get_connection() as conn:
        customers = await conn.fetch(
            """SELECT id FROM customers
               WHERE user_id = $1
               AND id NOT IN (
                   SELECT source_customer_id FROM acc_contacts
                   WHERE user_id = $1 AND source_customer_id IS NOT NULL
               )""",
            uid,
        )

    synced = 0
    errors = 0
    for row in customers:
        try:
            result = await sync_customer_to_contact(
                customer_id=row["id"],
                user_id=uid,
                branch_id=user.branch_id,
            )
            if result.get("status") == "synced":
                synced += 1
        except Exception:
            errors += 1

    return {"synced": synced, "errors": errors, "total": len(customers)}


# ──────────────────────────────────────────────────────────────
# Day Book (Daily Journal)
# ──────────────────────────────────────────────────────────────

class DayBookRequest(BaseModel):
    target_date: Optional[date] = None  # defaults to today


@router.post("/daybook")
async def create_day_book(
    body: DayBookRequest = DayBookRequest(),
    user: UserContext = Depends(_auth),
):
    """Generate (or regenerate) the day book journal for a specific date."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    target = body.target_date or date.today()
    return await generate_day_book(
        target_date=target,
        user_id=uid,
        branch_id=user.branch_id,
    )


@router.get("/daybook/today")
async def get_today_daybook(user: UserContext = Depends(_auth)):
    """Get or generate today's day book summary."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    return await generate_day_book(
        target_date=date.today(),
        user_id=uid,
        branch_id=user.branch_id,
    )


@router.get("/daybook")
async def list_day_books(
    user: UserContext = Depends(_auth),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
):
    """List existing day book journal entries, optionally filtered by date range."""
    uid = user.owner_id if user.is_branch_user else user.user_id

    conditions = ["user_id = $1", "reference_number LIKE 'DAYBOOK-%'"]
    params: list = [uid]

    if from_date:
        params.append(from_date)
        conditions.append(f"journal_date >= ${len(params)}")
    if to_date:
        params.append(to_date)
        conditions.append(f"journal_date <= ${len(params)}")

    where = " AND ".join(conditions)
    offset = (page - 1) * per_page

    async with get_connection() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM acc_journals WHERE {where}", *params,
        )
        rows = await conn.fetch(
            f"""SELECT journal_id, journal_number, journal_date,
                       reference_number, notes, total, status,
                       created_at
                FROM acc_journals
                WHERE {where}
                ORDER BY journal_date DESC
                LIMIT {per_page} OFFSET {offset}""",
            *params,
        )

    return {
        "daybooks": [
            {
                "journal_id": str(r["journal_id"]),
                "journal_number": r["journal_number"],
                "date": str(r["journal_date"]),
                "notes": r["notes"],
                "total": float(r["total"] or 0),
                "status": r["status"],
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ],
        "total": total or 0,
        "page": page,
        "per_page": per_page,
    }
