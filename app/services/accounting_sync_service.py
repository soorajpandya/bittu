"""Accounting Sync Service — bridges restaurant operations with accounting.

Listens for domain events (order paid, payment completed, refund issued, etc.)
and automatically creates the corresponding accounting records so both systems
stay in sync without manual double-entry.

Integration points:
  1. PAYMENT_COMPLETED  → acc_invoices + acc_line_items + acc_customer_payments
  2. PAYMENT_REFUNDED   → acc_credit_notes + acc_creditnote_refunds
  3. ORDER_CANCELLED    → voids or reverses any synced invoice
  4. PAYMENT_COMPLETED  → acc_journals day-book entry (daily summary)
  5. Inventory PO received → acc_bills (future)
  6. Customer sync      → acc_contacts

Also exposes helpers that can be called directly from API endpoints for
manual / bulk reconciliation.
"""
import uuid
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from app.core.database import get_connection
from app.core.logging import get_logger
from app.core.events import (
    DomainEvent, subscribe,
    PAYMENT_COMPLETED, PAYMENT_REFUNDED, ORDER_CANCELLED,
)

logger = get_logger(__name__)


# ─── Helpers ────────────────────────────────────────────────────


def _uid(user_id: str, branch_id: Optional[str]) -> dict:
    """Build tenant filter params."""
    return {"user_id": user_id, "branch_id": branch_id}


async def _get_or_create_contact(
    conn,
    *,
    user_id: str,
    branch_id: Optional[str],
    customer_id: Optional[int] = None,
    customer_phone: Optional[str] = None,
    customer_name: Optional[str] = None,
) -> str:
    """Find or create an acc_contacts record linked to a restaurant customer.

    Returns contact_id (UUID string).
    """
    # Try to find existing contact linked to this restaurant customer_id
    if customer_id:
        row = await conn.fetchrow(
            """SELECT contact_id FROM acc_contacts
               WHERE user_id = $1 AND source_customer_id = $2""",
            user_id, customer_id,
        )
        if row:
            return str(row["contact_id"])

    # Try by phone
    if customer_phone:
        row = await conn.fetchrow(
            """SELECT contact_id FROM acc_contacts
               WHERE user_id = $1 AND phone = $2 AND contact_type = 'customer'""",
            user_id, customer_phone,
        )
        if row:
            # Link it if not already linked
            if customer_id:
                await conn.execute(
                    "UPDATE acc_contacts SET source_customer_id = $1 WHERE contact_id = $2",
                    customer_id, row["contact_id"],
                )
            return str(row["contact_id"])

    # Create new contact
    contact_id = str(uuid.uuid4())
    name = customer_name or "Walk-in Customer"
    await conn.execute(
        """INSERT INTO acc_contacts
           (contact_id, contact_name, contact_type, phone, source_customer_id,
            user_id, branch_id, created_at, updated_at)
           VALUES ($1, $2, 'customer', $3, $4, $5, $6, now(), now())""",
        contact_id, name, customer_phone, customer_id, user_id, branch_id,
    )
    return contact_id


async def _get_default_income_account(conn, user_id: str) -> Optional[str]:
    """Get the default 'Sales' / 'Income' account for auto-created invoices."""
    row = await conn.fetchrow(
        """SELECT account_id FROM acc_chart_of_accounts
           WHERE user_id = $1 AND account_type = 'income'
           ORDER BY created_at LIMIT 1""",
        user_id,
    )
    return str(row["account_id"]) if row else None


async def _next_invoice_number(conn, user_id: str) -> str:
    """Generate the next invoice number like INV-00001."""
    row = await conn.fetchval(
        """SELECT COUNT(*) FROM acc_invoices WHERE user_id = $1""",
        user_id,
    )
    seq = (row or 0) + 1
    return f"INV-{seq:05d}"


async def _next_creditnote_number(conn, user_id: str) -> str:
    """Generate the next credit note number like CN-00001."""
    row = await conn.fetchval(
        """SELECT COUNT(*) FROM acc_credit_notes WHERE user_id = $1""",
        user_id,
    )
    seq = (row or 0) + 1
    return f"CN-{seq:05d}"


# ─── Core Sync: Order Paid → Invoice ───────────────────────────


async def sync_payment_to_invoice(
    *,
    order_id: str,
    payment_id: str,
    amount: float,
    user_id: str,
    branch_id: Optional[str] = None,
    restaurant_id: Optional[str] = None,
) -> dict:
    """Create an accounting invoice + line items + customer payment from a completed order.

    Idempotent — if an invoice already exists for this order_id, returns it.
    """
    async with get_connection() as conn:
        # Idempotency: check if already synced
        existing = await conn.fetchrow(
            "SELECT invoice_id FROM acc_invoices WHERE source_order_id = $1 AND user_id = $2",
            order_id, user_id,
        )
        if existing:
            logger.info("invoice_already_synced", order_id=order_id, invoice_id=str(existing["invoice_id"]))
            return {"invoice_id": str(existing["invoice_id"]), "status": "already_synced"}

        # Fetch order details
        order = await conn.fetchrow(
            "SELECT * FROM orders WHERE id = $1", order_id,
        )
        if not order:
            logger.warning("order_not_found_for_sync", order_id=order_id)
            return {"error": "order_not_found"}

        # Fetch order items
        items = await conn.fetch(
            "SELECT * FROM order_items WHERE order_id = $1", order_id,
        )

        # Get or create accounting contact for this customer
        customer_name = None
        customer_phone = None
        if order.get("customer_id"):
            cust = await conn.fetchrow(
                "SELECT name, phone FROM customers WHERE id = $1",
                order["customer_id"],
            )
            if cust:
                customer_name = cust["name"]
                customer_phone = cust.get("phone")

        contact_id = await _get_or_create_contact(
            conn,
            user_id=user_id,
            branch_id=branch_id,
            customer_id=order.get("customer_id"),
            customer_phone=customer_phone or order.get("delivery_phone"),
            customer_name=customer_name,
        )

        # Create invoice
        invoice_id = str(uuid.uuid4())
        invoice_number = await _next_invoice_number(conn, user_id)
        income_account = await _get_default_income_account(conn, user_id)

        today = date.today()
        subtotal = float(order.get("subtotal", 0))
        tax_total = float(order.get("tax_amount", 0))
        discount = float(order.get("discount_amount", 0))
        total = float(order.get("total_amount", amount))

        await conn.execute(
            """INSERT INTO acc_invoices
               (invoice_id, invoice_number, customer_id, date, due_date,
                sub_total, tax_total, total, balance, payment_made,
                discount, status, source_order_id, source_payment_id,
                user_id, branch_id, created_at, updated_at)
               VALUES ($1,$2,$3,$4,$4, $5,$6,$7, 0, $7, $8, 'paid',
                       $9, $10, $11, $12, now(), now())""",
            invoice_id, invoice_number, contact_id, today,
            subtotal, tax_total, total, discount,
            order_id, payment_id, user_id, branch_id,
        )

        # Create line items
        for idx, item in enumerate(items):
            li_id = str(uuid.uuid4())
            await conn.execute(
                """INSERT INTO acc_line_items
                   (line_item_id, parent_id, parent_type,
                    name, description, rate, quantity, item_total, item_order,
                    user_id, branch_id, created_at, updated_at)
                   VALUES ($1, $2, 'invoice', $3, $4, $5, $6, $7, $8,
                           $9, $10, now(), now())""",
                li_id, invoice_id,
                item.get("item_name", "Item"),
                item.get("notes"),
                float(item.get("unit_price", 0)),
                item.get("quantity", 1),
                float(item.get("total_price", 0)),
                idx,
                user_id, branch_id,
            )

        # Create customer payment record
        cp_id = str(uuid.uuid4())
        await conn.execute(
            """INSERT INTO acc_customer_payments
               (payment_id, customer_id, payment_mode, amount, date,
                reference_number, description,
                invoices, user_id, branch_id, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, now(), now())""",
            cp_id, contact_id, "pos",  # payment_mode from restaurant
            total, today,
            f"Order #{order_id[:8]}",
            f"Auto-synced from order {order_id}",
            json.dumps([{"invoice_id": invoice_id, "amount_applied": total}]),
            user_id, branch_id,
        )

        logger.info(
            "payment_synced_to_invoice",
            order_id=order_id, invoice_id=invoice_id,
            payment_id=payment_id, amount=total,
        )

        return {
            "invoice_id": invoice_id,
            "invoice_number": invoice_number,
            "customer_payment_id": cp_id,
            "contact_id": contact_id,
            "status": "synced",
        }


# ─── Core Sync: Refund → Credit Note ──────────────────────────


async def sync_refund_to_creditnote(
    *,
    order_id: str,
    payment_id: str,
    amount: float,
    user_id: str,
    branch_id: Optional[str] = None,
) -> dict:
    """Create a credit note when a payment is refunded."""
    async with get_connection() as conn:
        # Find the related invoice
        inv = await conn.fetchrow(
            "SELECT invoice_id, customer_id FROM acc_invoices WHERE source_order_id = $1 AND user_id = $2",
            order_id, user_id,
        )
        if not inv:
            logger.warning("no_invoice_for_refund_sync", order_id=order_id)
            return {"error": "no_invoice_found"}

        # Idempotency check
        existing = await conn.fetchrow(
            "SELECT creditnote_id FROM acc_credit_notes WHERE source_order_id = $1 AND user_id = $2",
            order_id, user_id,
        )
        if existing:
            return {"creditnote_id": str(existing["creditnote_id"]), "status": "already_synced"}

        cn_id = str(uuid.uuid4())
        cn_number = await _next_creditnote_number(conn, user_id)
        today = date.today()

        await conn.execute(
            """INSERT INTO acc_credit_notes
               (creditnote_id, creditnote_number, customer_id, date,
                total, balance, status, source_order_id,
                user_id, branch_id, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $5, 'open', $6, $7, $8, now(), now())""",
            cn_id, cn_number, str(inv["customer_id"]), today,
            amount, order_id, user_id, branch_id,
        )

        # Apply credit note to original invoice
        cni_id = str(uuid.uuid4())
        await conn.execute(
            """INSERT INTO acc_creditnote_invoices
               (creditnote_invoice_id, creditnote_id, invoice_id,
                amount_applied, date, user_id, branch_id, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, now())""",
            cni_id, cn_id, str(inv["invoice_id"]),
            amount, today, user_id, branch_id,
        )

        # Update invoice balance
        await conn.execute(
            """UPDATE acc_invoices SET balance = balance + $1,
               payment_made = payment_made - $1, updated_at = now()
               WHERE invoice_id = $2""",
            amount, str(inv["invoice_id"]),
        )

        logger.info(
            "refund_synced_to_creditnote",
            order_id=order_id, creditnote_id=cn_id, amount=amount,
        )

        return {
            "creditnote_id": cn_id,
            "creditnote_number": cn_number,
            "status": "synced",
        }


# ─── Core Sync: Order Cancelled → Void Invoice ────────────────


async def sync_order_cancelled(
    *,
    order_id: str,
    user_id: str,
    branch_id: Optional[str] = None,
) -> dict:
    """Void the accounting invoice when an order is cancelled."""
    async with get_connection() as conn:
        inv = await conn.fetchrow(
            "SELECT invoice_id, status FROM acc_invoices WHERE source_order_id = $1 AND user_id = $2",
            order_id, user_id,
        )
        if not inv:
            return {"status": "no_invoice"}

        if inv["status"] == "void":
            return {"invoice_id": str(inv["invoice_id"]), "status": "already_void"}

        await conn.execute(
            """UPDATE acc_invoices SET status = 'void', updated_at = now()
               WHERE invoice_id = $1""",
            inv["invoice_id"],
        )

        logger.info("order_cancel_voided_invoice", order_id=order_id, invoice_id=str(inv["invoice_id"]))

        return {"invoice_id": str(inv["invoice_id"]), "status": "voided"}


# ─── Day Book (Daily Journal) ──────────────────────────────────


async def _get_or_create_account(
    conn, *, user_id: str, branch_id: Optional[str],
    account_name: str, account_type: str, account_code: str,
) -> str:
    """Find or create a chart-of-accounts entry. Returns account_id."""
    row = await conn.fetchrow(
        """SELECT account_id FROM acc_chart_of_accounts
           WHERE user_id = $1 AND account_name = $2 AND account_type = $3""",
        user_id, account_name, account_type,
    )
    if row:
        return str(row["account_id"])
    acct_id = str(uuid.uuid4())
    await conn.execute(
        """INSERT INTO acc_chart_of_accounts
           (account_id, account_name, account_code, account_type,
            is_user_created, is_system_account, user_id, branch_id, created_at, updated_at)
           VALUES ($1,$2,$3,$4, false, true, $5,$6, now(), now())""",
        acct_id, account_name, account_code, account_type, user_id, branch_id,
    )
    return acct_id


async def _next_journal_number(conn, user_id: str) -> str:
    seq = (await conn.fetchval(
        "SELECT COUNT(*) FROM acc_journals WHERE user_id = $1", user_id,
    ) or 0) + 1
    return f"JRN-{seq:05d}"


async def generate_day_book(
    *,
    target_date: date,
    user_id: str,
    branch_id: Optional[str] = None,
) -> dict:
    """Create (or update) a daily summary journal entry for the given date.

    The day-book journal aggregates all completed payments for the day into
    a balanced double-entry journal:
        Debit  Cash / Bank          (total collected)
        Credit Sales Revenue         (net sales)
        Credit Tax Payable           (tax collected)
        Credit Discount Given (debit, contra-revenue)  (if any discount)

    Idempotent — regenerates the journal for that date if called again.
    """
    async with get_connection() as conn:
        # Fetch day's completed payments with order details
        rows = await conn.fetch(
            """SELECT o.id AS order_id, p.id AS payment_id, p.method,
                      p.amount AS payment_amount,
                      o.subtotal, o.tax_amount, o.discount_amount, o.total_amount
               FROM orders o
               JOIN payments p ON p.order_id = o.id
               WHERE o.user_id = $1
                 AND p.status = 'completed'
                 AND p.paid_at::date = $2
               ORDER BY p.paid_at""",
            user_id, target_date,
        )

        if not rows:
            return {"status": "no_orders", "date": str(target_date)}

        # Aggregate totals
        total_cash = 0.0
        total_online = 0.0
        total_sales = 0.0
        total_tax = 0.0
        total_discount = 0.0
        order_count = len(rows)

        for r in rows:
            amt = float(r["payment_amount"] or r["total_amount"] or 0)
            method = (r["method"] or "cash").lower()
            if method == "cash":
                total_cash += amt
            else:
                total_online += amt
            total_sales += float(r["subtotal"] or 0)
            total_tax += float(r["tax_amount"] or 0)
            total_discount += float(r["discount_amount"] or 0)

        total_collected = round(total_cash + total_online, 2)
        total_sales = round(total_sales, 2)
        total_tax = round(total_tax, 2)
        total_discount = round(total_discount, 2)
        total_cash = round(total_cash, 2)
        total_online = round(total_online, 2)

        # Ensure required chart-of-accounts entries exist
        cash_acct = await _get_or_create_account(
            conn, user_id=user_id, branch_id=branch_id,
            account_name="Cash on Hand", account_type="asset", account_code="1001",
        )
        bank_acct = await _get_or_create_account(
            conn, user_id=user_id, branch_id=branch_id,
            account_name="Bank (Online Payments)", account_type="asset", account_code="1002",
        )
        sales_acct = await _get_or_create_account(
            conn, user_id=user_id, branch_id=branch_id,
            account_name="Sales Revenue", account_type="income", account_code="4001",
        )
        tax_acct = await _get_or_create_account(
            conn, user_id=user_id, branch_id=branch_id,
            account_name="Tax Payable", account_type="liability", account_code="2001",
        )
        discount_acct = await _get_or_create_account(
            conn, user_id=user_id, branch_id=branch_id,
            account_name="Discount Given", account_type="expense", account_code="5001",
        )

        # Delete existing day-book journal for this date (idempotent regeneration)
        existing = await conn.fetchrow(
            """SELECT journal_id FROM acc_journals
               WHERE user_id = $1 AND reference_number = $2""",
            user_id, f"DAYBOOK-{target_date.isoformat()}",
        )
        if existing:
            old_id = existing["journal_id"]
            await conn.execute(
                "DELETE FROM acc_line_items WHERE parent_id = $1 AND parent_type = 'journal'",
                old_id,
            )
            await conn.execute("DELETE FROM acc_journals WHERE journal_id = $1", old_id)

        # Create journal
        journal_id = str(uuid.uuid4())
        journal_number = await _next_journal_number(conn, user_id)

        await conn.execute(
            """INSERT INTO acc_journals
               (journal_id, journal_number, journal_date, reference_number,
                notes, journal_type, status, total,
                user_id, branch_id, created_at, updated_at)
               VALUES ($1,$2,$3,$4,$5,'both','published',$6,$7,$8,now(),now())""",
            journal_id, journal_number, target_date,
            f"DAYBOOK-{target_date.isoformat()}",
            f"Day Book — {target_date.strftime('%d %b %Y')} | {order_count} orders | ₹{total_collected:,.2f}",
            total_collected, user_id, branch_id,
        )

        # Build balanced line items
        line_order = 0

        async def _add_line(acct_id: str, name: str, dc: str, amt: float):
            nonlocal line_order
            if amt <= 0:
                return
            li_id = str(uuid.uuid4())
            await conn.execute(
                """INSERT INTO acc_line_items
                   (line_item_id, parent_id, parent_type, account_id,
                    name, debit_or_credit, amount, item_order,
                    user_id, branch_id, created_at)
                   VALUES ($1,$2,'journal',$3,$4,$5,$6,$7,$8,$9,now())""",
                li_id, journal_id, acct_id, name, dc, amt, line_order,
                user_id, branch_id,
            )
            line_order += 1

        # DEBIT side — assets (money received)
        if total_cash > 0:
            await _add_line(cash_acct, "Cash Sales", "debit", total_cash)
        if total_online > 0:
            await _add_line(bank_acct, "Online Sales", "debit", total_online)

        # DEBIT side — discount is contra-revenue
        if total_discount > 0:
            await _add_line(discount_acct, "Discount Given", "debit", total_discount)

        # CREDIT side — revenue + tax
        await _add_line(sales_acct, "Sales Revenue", "credit", total_sales)
        if total_tax > 0:
            await _add_line(tax_acct, "Tax Collected", "credit", total_tax)

        logger.info(
            "day_book_generated",
            date=str(target_date), journal_id=journal_id, orders=order_count,
            cash=total_cash, online=total_online, sales=total_sales,
            tax=total_tax, discount=total_discount,
        )

        return {
            "journal_id": journal_id,
            "journal_number": journal_number,
            "date": str(target_date),
            "order_count": order_count,
            "total_collected": total_collected,
            "cash": total_cash,
            "online": total_online,
            "sales": total_sales,
            "tax": total_tax,
            "discount": total_discount,
            "status": "generated",
        }


async def auto_close_day_book(
    *,
    user_id: str,
    branch_id: Optional[str] = None,
) -> dict:
    """Generate day book for yesterday (typically called by a scheduler or at EOD)."""
    from datetime import timedelta
    yesterday = date.today() - timedelta(days=1)
    return await generate_day_book(
        target_date=yesterday, user_id=user_id, branch_id=branch_id,
    )


# ─── Customer Sync ─────────────────────────────────────────────


async def sync_customer_to_contact(
    *,
    customer_id: int,
    user_id: str,
    branch_id: Optional[str] = None,
) -> dict:
    """Sync a restaurant customer to an accounting contact."""
    async with get_connection() as conn:
        cust = await conn.fetchrow(
            "SELECT * FROM customers WHERE id = $1", customer_id,
        )
        if not cust:
            return {"error": "customer_not_found"}

        contact_id = await _get_or_create_contact(
            conn,
            user_id=user_id,
            branch_id=branch_id,
            customer_id=customer_id,
            customer_phone=cust.get("phone"),
            customer_name=cust.get("name"),
        )

        # Update contact details from customer record
        await conn.execute(
            """UPDATE acc_contacts SET
               contact_name = COALESCE($1, contact_name),
               email = COALESCE($2, email),
               phone = COALESCE($3, phone),
               updated_at = now()
               WHERE contact_id = $4""",
            cust.get("name"), cust.get("email"), cust.get("phone"), contact_id,
        )

        return {"contact_id": contact_id, "status": "synced"}


# ─── Bulk Sync (manual reconciliation) ─────────────────────────


async def bulk_sync_orders(
    *,
    user_id: str,
    branch_id: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> dict:
    """Sync all paid orders that don't yet have accounting invoices.

    Used for initial setup or catch-up after enabling accounting.
    """
    async with get_connection() as conn:
        conditions = ["o.user_id = $1", "p.status = 'completed'"]
        params = [user_id]

        if from_date:
            params.append(from_date)
            conditions.append(f"o.created_at >= ${len(params)}")
        if to_date:
            params.append(to_date)
            conditions.append(f"o.created_at <= ${len(params)}")

        where = " AND ".join(conditions)

        rows = await conn.fetch(
            f"""SELECT o.id AS order_id, p.id AS payment_id,
                       p.amount, o.user_id, o.branch_id, o.restaurant_id
                FROM orders o
                JOIN payments p ON p.order_id = o.id
                LEFT JOIN acc_invoices ai ON ai.source_order_id = o.id::text
                WHERE {where} AND ai.invoice_id IS NULL
                ORDER BY o.created_at""",
            *params,
        )

    synced = 0
    errors = 0
    for row in rows:
        try:
            result = await sync_payment_to_invoice(
                order_id=str(row["order_id"]),
                payment_id=str(row["payment_id"]),
                amount=float(row["amount"]),
                user_id=row["user_id"],
                branch_id=str(row["branch_id"]) if row.get("branch_id") else None,
                restaurant_id=str(row["restaurant_id"]) if row.get("restaurant_id") else None,
            )
            if result.get("status") in ("synced", "already_synced"):
                synced += 1
            else:
                errors += 1
        except Exception:
            logger.exception("bulk_sync_error", order_id=str(row["order_id"]))
            errors += 1

    return {"synced": synced, "errors": errors, "total": len(rows)}


# ─── Sync Status ───────────────────────────────────────────────


async def get_sync_status(
    *,
    user_id: str,
    branch_id: Optional[str] = None,
) -> dict:
    """Return a dashboard summary of sync status between restaurant + accounting."""
    async with get_connection() as conn:
        total_orders = await conn.fetchval(
            """SELECT COUNT(*) FROM orders o
               JOIN payments p ON p.order_id = o.id
               WHERE o.user_id = $1 AND p.status = 'completed'""",
            user_id,
        )
        synced_orders = await conn.fetchval(
            """SELECT COUNT(*) FROM acc_invoices
               WHERE user_id = $1 AND source_order_id IS NOT NULL""",
            user_id,
        )
        unsynced = (total_orders or 0) - (synced_orders or 0)

        total_revenue = await conn.fetchval(
            """SELECT COALESCE(SUM(p.amount), 0) FROM payments p
               JOIN orders o ON o.id = p.order_id
               WHERE o.user_id = $1 AND p.status = 'completed'""",
            user_id,
        )
        acc_revenue = await conn.fetchval(
            """SELECT COALESCE(SUM(total), 0) FROM acc_invoices
               WHERE user_id = $1 AND status != 'void'""",
            user_id,
        )

        return {
            "total_paid_orders": total_orders or 0,
            "synced_invoices": synced_orders or 0,
            "unsynced_orders": max(unsynced, 0),
            "restaurant_revenue": float(total_revenue or 0),
            "accounting_revenue": float(acc_revenue or 0),
            "is_in_sync": unsynced <= 0,
        }


# ─── Event Handlers ────────────────────────────────────────────


async def _on_payment_completed(event: DomainEvent):
    """Auto-create accounting invoice + update day book when a restaurant payment completes."""
    try:
        await sync_payment_to_invoice(
            order_id=event.payload.get("order_id", ""),
            payment_id=event.payload.get("payment_id", ""),
            amount=float(event.payload.get("amount", 0)),
            user_id=event.user_id or "",
            branch_id=event.branch_id,
            restaurant_id=event.restaurant_id,
        )
    except Exception:
        logger.exception("auto_sync_payment_failed", payload=event.payload)

    # Regenerate today's day book so it includes this payment
    try:
        await generate_day_book(
            target_date=date.today(),
            user_id=event.user_id or "",
            branch_id=event.branch_id,
        )
    except Exception:
        logger.exception("auto_daybook_failed", payload=event.payload)


async def _on_payment_refunded(event: DomainEvent):
    """Auto-create credit note when a restaurant payment is refunded."""
    try:
        await sync_refund_to_creditnote(
            order_id=event.payload.get("order_id", ""),
            payment_id=event.payload.get("payment_id", ""),
            amount=float(event.payload.get("amount", 0)),
            user_id=event.user_id or "",
            branch_id=event.branch_id,
        )
    except Exception:
        logger.exception("auto_sync_refund_failed", payload=event.payload)


async def _on_order_cancelled(event: DomainEvent):
    """Void the accounting invoice when a restaurant order is cancelled."""
    try:
        await sync_order_cancelled(
            order_id=event.payload.get("order_id", ""),
            user_id=event.user_id or "",
            branch_id=event.branch_id,
        )
    except Exception:
        logger.exception("auto_sync_cancel_failed", payload=event.payload)


def register_accounting_handlers():
    """Register all accounting event handlers. Call once at startup."""
    subscribe(PAYMENT_COMPLETED, _on_payment_completed)
    subscribe(PAYMENT_REFUNDED, _on_payment_refunded)
    subscribe(ORDER_CANCELLED, _on_order_cancelled)
    logger.info("accounting_sync_handlers_registered")
