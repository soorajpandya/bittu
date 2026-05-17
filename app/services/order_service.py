"""
Order Management Service — the central nervous system of BITTU.

Concurrency control:
  - Distributed lock per order for mutations
  - SERIALIZABLE transactions for financial state changes
  - DB-backed, auth-scoped idempotency keys to prevent double-creation

Data flow:
  Order Created → Inventory Deducted → Kitchen Order Created → Payment Initiated
  → Payment Completed → Order Served/Delivered

Abuse prevention:
  - Orders can only be cancelled before `preparing` (grace window)
  - Only owners/managers can cancel after confirmation
  - Order amounts are recalculated server-side (never trust client totals)

Date/timezone contract:
  - All timestamps stored in UTC (TIMESTAMPTZ).
  - from_date/to_date query filters are interpreted as IST calendar days:
      from_date inclusive  → created_at >= 00:00 IST on from_date  (UTC)
      to_date   inclusive  → created_at <  00:00 IST on (to_date+1) (UTC)
"""
import json
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import asyncpg

from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction, get_transaction
from app.core.redis import DistributedLock, LockError
from app.core.state_machines import OrderStatus, validate_order_transition
from app.core.events import (
    DomainEvent, emit_and_publish,
    ORDER_CREATED, ORDER_CONFIRMED, ORDER_STATUS_CHANGED, ORDER_CANCELLED,
)
from app.core.tenant import tenant_where_clause, tenant_insert_fields
from app.core.exceptions import (
    NotFoundError, ConflictError, ForbiddenError,
    LockAcquisitionError, ValidationError, CheckoutError,
)
from app.core.ist import ist_range_utc
from app.core.logging import get_logger

logger = get_logger(__name__)


class OrderService:

    # ── Item lookup helper ──

    async def _lookup_item(self, conn, item_id, item_name, user_id):
        """Look up an item by ID or name. Tenant-scoped on BOTH branches.

        SECURITY: by-id lookup MUST include `user_id = $2` — otherwise a malicious
        client can quote any item_id from any merchant and have its price/availability
        accepted server-side (cross-tenant pricing fraud — audit finding A3.2).
        """
        row = None
        if item_id:
            row = await conn.fetchrow(
                """SELECT "Item_ID", "Item_Name", price, "Available_Status"
                   FROM items WHERE "Item_ID" = $1 AND user_id = $2""",
                item_id, user_id,
            )
        if not row and item_name:
            row = await conn.fetchrow(
                """SELECT "Item_ID", "Item_Name", price, "Available_Status"
                   FROM items WHERE "Item_Name" = $1 AND user_id = $2""",
                item_name, user_id,
            )
        if not row:
            raise NotFoundError("Item", str(item_id or item_name))
        if not row["Available_Status"]:
            raise ValidationError(f"Item '{row['Item_Name']}' is currently unavailable")
        return row

    # ── CHECKOUT (idempotent, full-response) ──

    async def checkout(
        self,
        user: UserContext,
        items: list[dict],
        source: str = "pos",
        order_type: Optional[str] = None,
        payment_method: Optional[str] = None,
        total_amount: Optional[float] = None,
        customer_id: Optional[int] = None,
        customer_name: Optional[str] = None,
        customer_phone: Optional[str] = None,
        table_number: Optional[str] = None,
        delivery_address: Optional[str] = None,
        coupon_id: Optional[int] = None,
        coupon_code: Optional[str] = None,
        notes: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """
        Idempotent checkout: create an order and return full response with items[].

        Idempotency contract
        --------------------
        - Scope: (idempotency_key, owner_user_id) — branch staff submissions are
          scoped to the owner, so the same key cannot create two orders even if two
          different branch users submit simultaneously.
        - Storage: durable DB table checkout_idempotency (not Redis) so the record
          survives Redis restarts and can be audited.
        - Atomicity: the idempotency record is inserted INSIDE the same SERIALIZABLE
          transaction as the order.  If two concurrent requests race, PostgreSQL's
          unique constraint raises UniqueViolationError on one of them, rolling back
          its transaction.  The service catches this and returns the committed record.
        - TTL: 24 hours.  After that the same key may create a new order.

        Returns
        -------
        Full order dict including items[].  Replayed responses include idempotent=True.
        """
        t0 = time.perf_counter()
        owner_id = user.owner_id if user.is_branch_user else user.user_id

        # ── Fast path: check existing idempotency record ──────────────────────
        if idempotency_key:
            async with get_connection() as conn:
                existing = await conn.fetchrow(
                    """
                    SELECT response_payload
                    FROM   checkout_idempotency
                    WHERE  idempotency_key = $1
                    AND    user_id         = $2
                    AND    expires_at      > NOW()
                    """,
                    idempotency_key,
                    owner_id,
                )
            if existing:
                payload = dict(existing["response_payload"])
                payload["idempotent"] = True
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                logger.info(
                    "checkout_replayed",
                    idempotency_key=idempotency_key,
                    order_id=payload.get("id"),
                    user_id=user.user_id,
                    owner_id=owner_id,
                    branch_id=str(user.branch_id) if user.branch_id else None,
                    restaurant_id=str(user.restaurant_id) if user.restaurant_id else None,
                    outcome="replayed",
                    latency_ms=latency_ms,
                )
                return payload

        if not items:
            raise ValidationError("Order must contain at least one item")

        tenant = tenant_insert_fields(user)
        # `source` is the order_source enum (pos/app/qr_table/online/delivery_partner).
        # `order_type` is a UX category (dine_in/takeaway/delivery) and lives in metadata only.
        # `source` ALWAYS wins over order_type. We only fall back to order_type when
        # the client did not send `source` at all (legacy clients).
        _ALLOWED = {"pos", "app", "qr_table", "online", "delivery_partner"}
        _SOURCE_ALIASES = {
            "dine_in":   "qr_table",
            "dinein":    "qr_table",
            "dine-in":   "qr_table",
            "table":     "qr_table",
            "qr":        "qr_table",
            "takeaway":  "pos",
            "take_away": "pos",
            "counter":   "pos",
            "website":   "online",
            "web":       "online",
            "mobile":    "app",
            "delivery":  "delivery_partner",
        }

        def _normalise(v):
            v = str(v or "").strip().lower()
            return _SOURCE_ALIASES.get(v, v)

        primary = _normalise(source)
        if primary in _ALLOWED:
            source_value = primary
        else:
            fallback = _normalise(order_type)
            source_value = fallback if fallback in _ALLOWED else "pos"

        # ── Main transaction: create order + claim idempotency ────────────────
        try:
            response = await self._create_order_txn(
                tenant=tenant,
                user=user,
                items=items,
                source=source_value,
                payment_method=payment_method,
                customer_id=customer_id,
                customer_name=customer_name,
                customer_phone=customer_phone,
                table_number=table_number,
                delivery_address=delivery_address,
                delivery_phone=customer_phone,
                coupon_id=coupon_id,
                notes=notes,
                idempotency_key=idempotency_key,
                owner_id=owner_id,
            )
        except asyncpg.exceptions.UniqueViolationError:
            # Race: concurrent request committed with same idempotency key.
            # Our transaction was rolled back — read and return the winner's record.
            if idempotency_key:
                async with get_connection() as conn:
                    existing = await conn.fetchrow(
                        """
                        SELECT response_payload
                        FROM   checkout_idempotency
                        WHERE  idempotency_key = $1
                        AND    user_id         = $2
                        """,
                        idempotency_key,
                        owner_id,
                    )
                if existing:
                    payload = dict(existing["response_payload"])
                    payload["idempotent"] = True
                    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                    logger.info(
                        "checkout_replayed_race",
                        idempotency_key=idempotency_key,
                        order_id=payload.get("id"),
                        user_id=user.user_id,
                        owner_id=owner_id,
                        outcome="replayed",
                        latency_ms=latency_ms,
                    )
                    return payload
            raise
        except asyncpg.PostgresError as exc:
            # Surface the asyncpg error code + message clearly so the failing
            # request can be diagnosed from logs without DB access.
            logger.error(
                "checkout_db_error",
                idempotency_key=idempotency_key,
                user_id=user.user_id,
                owner_id=owner_id,
                branch_id=str(user.branch_id) if user.branch_id else None,
                restaurant_id=str(user.restaurant_id) if user.restaurant_id else None,
                source=source_value,
                sqlstate=getattr(exc, "sqlstate", None),
                pg_error=type(exc).__name__,
                pg_message=str(exc),
                exc_info=True,
            )
            raise CheckoutError(f"Order creation failed: {type(exc).__name__}: {exc}") from exc

        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        logger.info(
            "checkout_committed",
            idempotency_key=idempotency_key,
            order_id=response.get("id"),
            total_amount=response.get("total_amount"),
            source=source_value,
            item_count=len(response.get("items", [])),
            user_id=user.user_id,
            owner_id=owner_id,
            branch_id=str(user.branch_id) if user.branch_id else None,
            restaurant_id=str(user.restaurant_id) if user.restaurant_id else None,
            outcome="committed",
            latency_ms=latency_ms,
        )

        # Emit domain event outside transaction
        await emit_and_publish(DomainEvent(
            event_type=ORDER_CREATED,
            payload={
                "order_id": response["id"],
                "total_amount": response.get("total_amount"),
                "source": source_value,
                "item_count": len(response.get("items", [])),
            },
            user_id=user.user_id,
            restaurant_id=user.restaurant_id,
            branch_id=user.branch_id,
        ))

        return response

    async def _create_order_txn(
        self,
        *,
        tenant: dict,
        user: UserContext,
        items: list[dict],
        source: str,
        payment_method: Optional[str],
        customer_id: Optional[int],
        customer_name: Optional[str],
        customer_phone: Optional[str],
        table_number: Optional[str],
        delivery_address: Optional[str],
        delivery_phone: Optional[str],
        coupon_id: Optional[int],
        notes: Optional[str],
        idempotency_key: Optional[str],
        owner_id: str,
    ) -> dict:
        """All DB writes for order creation in a single READ COMMITTED transaction.

        Note: switched from SERIALIZABLE to READ COMMITTED on 2026-05-08 to
        match the fix already applied to dinein_session_service and table_service
        on 2026-04-30 (commits 308172a, 579d5b9). SERIALIZABLE was raising
        intermittent 'could not serialize access' errors which surfaced as 500s
        because checkout() only catches UniqueViolationError.

        Concurrency is still safe:
          - The unique constraint on (idempotency_key, user_id) handles duplicate
            checkouts and is caught explicitly above.
          - Item / variant / addon reads are price snapshots only — no stock
            deduction happens here, so dirty-read style races are not possible.
        """
        async with get_transaction() as conn:
            # Server-side price calculation — NEVER trust client prices
            subtotal = Decimal("0")
            order_items_data = []

            for item_entry in items:
                item_name = item_entry.get("item_name")
                item_id_input = item_entry.get("item_id")
                quantity = item_entry.get("quantity", 1)
                variant_name = item_entry.get("variant_name")
                variant_id_input = item_entry.get("variant_id")
                addons = item_entry.get("addons", [])
                item_notes = item_entry.get("notes")

                if quantity < 1 or quantity > 100:
                    raise ValidationError(f"Invalid quantity: {quantity}")

                if variant_id_input:
                    row = await conn.fetchrow(
                        """
                        SELECT iv.id, iv.name, iv.price, i."Item_ID", i."Item_Name", i."Available_Status"
                        FROM item_variants iv
                        JOIN items i ON i."Item_ID" = iv.item_id
                        WHERE iv.id = $1 AND iv.is_active = true
                        """,
                        variant_id_input,
                    )
                    if row:
                        unit_price = Decimal(str(row["price"]))
                        item_name_full = f"{row['Item_Name']} ({row['name']})"
                        item_id = row["Item_ID"]
                        variant_id = row["id"]
                    else:
                        raise NotFoundError("Variant", str(variant_id_input))
                elif variant_name:
                    row = await conn.fetchrow(
                        """
                        SELECT iv.id, iv.name, iv.price, i."Item_ID", i."Item_Name", i."Available_Status"
                        FROM item_variants iv
                        JOIN items i ON i."Item_ID" = iv.item_id
                        WHERE iv.name = $1 AND i."Item_Name" = $2 AND iv.is_active = true
                        """,
                        variant_name, item_name,
                    )
                    if row:
                        unit_price = Decimal(str(row["price"]))
                        item_name_full = f"{row['Item_Name']} ({row['name']})"
                        item_id = row["Item_ID"]
                        variant_id = row["id"]
                    else:
                        row = await self._lookup_item(conn, item_id_input, item_name, tenant["user_id"])
                        unit_price = Decimal(str(row["price"]))
                        item_name_full = row["Item_Name"]
                        item_id = row["Item_ID"]
                        variant_id = None
                else:
                    row = await self._lookup_item(conn, item_id_input, item_name, tenant["user_id"])
                    unit_price = Decimal(str(row["price"]))
                    item_name_full = row["Item_Name"]
                    item_id = row["Item_ID"]
                    variant_id = None
                    if not item_name:
                        item_name = row["Item_Name"]

                # Calculate addon prices
                addon_total = Decimal("0")
                if addons:
                    for addon in addons:
                        addon_row = await conn.fetchrow(
                            "SELECT price FROM item_addons WHERE id = $1 AND item_id = $2 AND is_active = true",
                            addon.get("id"), item_id,
                        )
                        if addon_row:
                            addon_total += Decimal(str(addon_row["price"]))

                line_total = (unit_price + addon_total) * quantity
                subtotal += line_total

                order_items_data.append({
                    "item_id": item_id,
                    "variant_id": variant_id,
                    "item_name": item_name_full if variant_id else (item_name or ""),
                    "quantity": quantity,
                    "unit_price": float(unit_price + addon_total),
                    "total_price": float(line_total),
                    "addons": addons or [],
                    "notes": item_notes,
                })

            # Apply coupon
            discount_amount = Decimal("0")
            if coupon_id:
                discount_amount = await self._calculate_coupon_discount(
                    conn, coupon_id, subtotal, customer_id, tenant["user_id"]
                )

            # Tax
            tax_pct = await self._get_tax_percentage(conn, user.restaurant_id)
            tax_amount = (subtotal - discount_amount) * tax_pct / 100
            total_amount = subtotal - discount_amount + tax_amount

            # Build metadata
            metadata: dict = {}
            if payment_method:
                metadata["payment_method"] = payment_method
            if customer_name:
                metadata["customer_name"] = customer_name

            order_id = str(uuid.uuid4())
            # Short human-readable order number (8-char prefix of UUID, uppercase)
            order_number = order_id[:8].upper()
            metadata["order_number"] = order_number

            await conn.execute(
                """
                INSERT INTO orders (
                    id, user_id, branch_id, restaurant_id, customer_id,
                    source, subtotal, tax_amount, discount_amount, total_amount,
                    status, table_number, delivery_address, delivery_phone,
                    coupon_id, notes, items, metadata
                ) VALUES (
                    $1, $2, $3, $4, $5, $6::order_source,
                    $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
                    $17::jsonb, $18::jsonb
                )
                """,
                order_id,
                tenant["user_id"],
                tenant.get("branch_id"),
                user.restaurant_id,
                customer_id,
                source,
                float(subtotal),
                float(tax_amount),
                float(discount_amount),
                float(total_amount),
                OrderStatus.PENDING.value,
                table_number,
                delivery_address,
                delivery_phone,
                coupon_id,
                notes,
                "[]",
                json.dumps(metadata),
            )

            # Insert order items
            for oi in order_items_data:
                item_id_val = oi["item_id"]
                await conn.execute(
                    """
                    INSERT INTO order_items (
                        order_id, item_id, variant_id, item_name,
                        quantity, unit_price, total_price, addons, notes, user_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
                    """,
                    order_id,
                    item_id_val,
                    oi["variant_id"],
                    oi["item_name"],
                    oi["quantity"],
                    oi["unit_price"],
                    oi["total_price"],
                    json.dumps(oi["addons"]),
                    oi["notes"],
                    tenant["user_id"],
                )

            # Build the full response now (within transaction, so all data is consistent)
            now_utc = datetime.now(timezone.utc).isoformat()

            # ── Auto-create payment row when payment_method is supplied ──
            # POS-side checkouts collect payment at the time of order, so a
            # payments row MUST exist for the wallet/recon engine to see it.
            # Cash-equivalent methods are marked completed immediately; online
            # methods (razorpay/online) stay pending until the gateway webhook fires.
            payment_id = None
            payment_status_value = None
            if payment_method:
                # Normalise to the payment_method enum (cash, upi, card, wallet, online).
                _PM_ALIASES = {
                    "cash": "cash", "counter": "cash", "cod": "cash",
                    "upi": "upi", "qr_pay": "upi", "qr": "upi", "qr_code": "upi",
                    "card": "card", "swipe": "card", "credit": "card", "debit": "card",
                    "wallet": "wallet",
                    "online": "online", "razorpay": "online", "gateway": "online", "netbanking": "online",
                }
                pm_norm = _PM_ALIASES.get(str(payment_method).strip().lower(), "cash")
                pay_status = "pending" if pm_norm == "online" else "completed"
                paid_at = None if pay_status == "pending" else datetime.now(timezone.utc)
                payment_id = str(uuid.uuid4())
                payment_status_value = pay_status
                try:
                    await conn.execute(
                        """
                        INSERT INTO payments (
                            id, order_id, restaurant_id, user_id, branch_id,
                            method, status, amount, currency, paid_at
                        ) VALUES (
                            $1, $2, $3, $4, $5,
                            $6::payment_method, $7::payment_status, $8, 'INR', $9
                        )
                        """,
                        payment_id,
                        order_id,
                        user.restaurant_id,
                        tenant["user_id"],
                        tenant.get("branch_id"),
                        pm_norm,
                        pay_status,
                        float(total_amount),
                        paid_at,
                    )
                    if pay_status == "completed":
                        await conn.execute(
                            "UPDATE orders SET status = 'Confirmed', updated_at = now() WHERE id = $1",
                            order_id,
                        )
                        # Mirror non-gateway receipts into the immutable
                        # merchant ledger.  Online/gateway payments stay
                        # 'pending' here and are intentionally NOT wired.
                        # Best-effort: never raises.
                        from app.services.merchant_ledger_integration import (
                            post_payment_received,
                        )
                        await post_payment_received(
                            merchant_id=user.restaurant_id,
                            payment_id=payment_id,
                            amount=total_amount,
                            method=pm_norm,
                            order_id=order_id,
                            branch_id=tenant.get("branch_id"),
                            actor_id=user.user_id,
                            conn=conn,
                        )
                        # Phase 2: also place into escrow (T+N hold).
                        from app.services.escrow_integration import (
                            hold_payment_in_escrow,
                        )
                        await hold_payment_in_escrow(
                            merchant_id=user.restaurant_id,
                            payment_id=payment_id,
                            amount=total_amount,
                            method=pm_norm,
                            order_id=order_id,
                            branch_id=tenant.get("branch_id"),
                            actor_id=user.user_id,
                            conn=conn,
                        )
                except Exception as exc:
                    # Don't fail checkout if the payments insert blows up — log and continue.
                    logger.warning(
                        "checkout_payment_insert_failed",
                        order_id=order_id,
                        method=pm_norm,
                        error=str(exc),
                    )
                    payment_id = None
                    payment_status_value = None

            response = {
                "id": order_id,
                "order_number": order_number,
                "status": "Confirmed" if payment_status_value == "completed" else OrderStatus.PENDING.value,
                "source": source,
                "order_type": source,
                "payment_method": payment_method,
                "payment_id": payment_id,
                "payment_status": payment_status_value,
                "subtotal": float(subtotal),
                "tax_amount": float(tax_amount),
                "discount_amount": float(discount_amount),
                "total_amount": float(total_amount),
                "customer_id": customer_id,
                "customer_name": customer_name,
                "customer_phone": customer_phone,
                "table_number": table_number,
                "delivery_address": delivery_address,
                "notes": notes,
                "created_at": now_utc,
                "updated_at": now_utc,
                "items": order_items_data,
                "idempotent": False,
            }

            # Claim idempotency WITHIN the same transaction
            if idempotency_key:
                await conn.execute(
                    """
                    INSERT INTO checkout_idempotency
                        (idempotency_key, user_id, order_id, response_payload, expires_at)
                    VALUES
                        ($1, $2, $3, $4::jsonb, NOW() + INTERVAL '24 hours')
                    """,
                    idempotency_key,
                    owner_id,
                    order_id,
                    json.dumps({k: v for k, v in response.items() if k != "idempotent"}),
                )
            # If UniqueViolationError is raised here, the entire transaction
            # (including the order INSERT) is rolled back by asyncpg automatically.

        # ── Razorpay payment-intent (online methods only) ──
        # Gateway calls MUST run OUTSIDE the serializable transaction.
        # Best-effort: a gateway failure is logged but does NOT roll back the
        # order — POS can retry via GET /payments/{order}/intent.
        if (
            payment_method
            and payment_id
            and payment_status_value == "pending"
            and pm_norm == "online"
        ):
            try:
                from app.services.razorpay.payment_intent import (
                    create_intent_for_order,
                )
                intent = await create_intent_for_order(
                    merchant_id=user.restaurant_id,
                    branch_id=tenant.get("branch_id"),
                    internal_order_id=order_id,
                    payment_id=payment_id,
                    amount=Decimal(str(total_amount)),
                    receipt=order_number,
                    customer_name=customer_name,
                    customer_phone=customer_phone,
                    customer_id=str(customer_id) if customer_id is not None else None,
                    created_by_user_id=user.user_id,
                    owner_user_id=user.owner_id if user.is_branch_user else user.user_id,
                    create_qr=True,
                )
                response["razorpay"] = intent.to_client_dict()
            except Exception as exc:
                logger.warning(
                    "checkout_rzp_intent_failed",
                    order_id=order_id,
                    payment_id=payment_id,
                    error=str(exc),
                )
                response["razorpay"] = {"error": "intent_creation_failed"}

        return response

    # ── CREATE ORDER (legacy — kept for backward compatibility) ──

    async def create_order(
        self,
        user: UserContext,
        items: list[dict],
        source: str = "pos",
        customer_id: Optional[int] = None,
        table_number: Optional[str] = None,
        delivery_address: Optional[str] = None,
        delivery_phone: Optional[str] = None,
        coupon_id: Optional[int] = None,
        notes: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """
        Legacy order-creation path (POST /orders).
        Delegates to checkout() for idempotency and full-response contract.
        """
        return await self.checkout(
            user=user,
            items=items,
            source=source,
            customer_id=customer_id,
            table_number=table_number,
            delivery_address=delivery_address,
            customer_phone=delivery_phone,
            coupon_id=coupon_id,
            notes=notes,
            idempotency_key=idempotency_key,
        )

    # ── UPDATE ORDER (GENERAL) ──

    async def update_order(
        self,
        user: UserContext,
        order_id: str,
        status: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> dict:
        """Update order fields. Handles status transition + notes."""
        async with get_connection() as conn:
            owner_id = user.owner_id if user.is_branch_user else user.user_id
            order = await conn.fetchrow(
                "SELECT id, status FROM orders WHERE id = $1 AND user_id = $2",
                order_id, owner_id,
            )
            if not order:
                raise NotFoundError("Order", order_id)

            if notes is not None:
                await conn.execute(
                    "UPDATE orders SET notes = $1, updated_at = now() WHERE id = $2",
                    notes, order_id,
                )

        if status:
            return await self.update_status(user=user, order_id=order_id, new_status=status)

        return await self.get_order_detail(user=user, order_id=order_id)

    async def apply_discount(
        self,
        user: UserContext,
        order_id: str,
        discount_percent: float,
        reason: Optional[str] = None,
    ) -> dict:
        """Apply percentage discount to an order with server-side recalculation."""
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        async with get_serializable_transaction() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, subtotal, tax_amount, total_amount, status, user_id, branch_id
                FROM orders
                WHERE id = $1 AND user_id = $2
                FOR UPDATE
                """,
                order_id,
                owner_id,
            )
            if not row:
                raise NotFoundError("Order", order_id)

            if user.is_branch_user and user.branch_id and row["branch_id"] and str(row["branch_id"]) != str(user.branch_id):
                raise ForbiddenError("Cross-branch access denied")

            if str(row["status"]).lower() in {"cancelled", "completed"}:
                raise ValidationError("Cannot apply discount to completed/cancelled orders")

            subtotal = Decimal(str(row["subtotal"] or 0))
            tax_amount = Decimal(str(row["tax_amount"] or 0))
            pct = Decimal(str(discount_percent))
            discount_amount = (subtotal * pct) / Decimal("100")
            new_total = subtotal - discount_amount + tax_amount
            if new_total < Decimal("0"):
                new_total = Decimal("0")

            notes = reason or ""
            await conn.execute(
                """
                UPDATE orders
                SET discount_amount = $1,
                    total_amount = $2,
                    notes = CASE WHEN COALESCE($3, '') = '' THEN notes ELSE CONCAT(COALESCE(notes, ''), '\n[discount] ', $3) END,
                    updated_at = NOW()
                WHERE id = $4
                """,
                float(discount_amount),
                float(new_total),
                notes,
                order_id,
            )

            updated = await conn.fetchrow(
                "SELECT id, subtotal, tax_amount, discount_amount, total_amount, status, updated_at FROM orders WHERE id = $1",
                order_id,
            )
            return dict(updated) if updated else {}

    # ── UPDATE ORDER STATUS ──

    async def update_status(
        self,
        user: UserContext,
        order_id: str,
        new_status: str,
    ) -> dict:
        """
        Transition order to a new status with concurrency protection.
        Uses distributed lock + state machine validation.
        """
        try:
            async with DistributedLock(f"order:{order_id}", timeout=10):
                async with get_serializable_transaction() as conn:
                    order = await conn.fetchrow(
                        """
                        SELECT id, status, user_id, branch_id, total_amount, source
                        FROM orders
                        WHERE id = $1 AND user_id = $2
                        FOR UPDATE
                        """,
                        order_id, user.owner_id if user.is_branch_user else user.user_id,
                    )

                    if not order:
                        raise NotFoundError("Order", order_id)

                    current = order["status"]
                    target = validate_order_transition(current, new_status)

                    # Idempotent: no-op if already in the target state
                    if target is None:
                        return {"id": order_id, "status": current}

                    # Business rules for cancellation
                    if target == OrderStatus.CANCELLED:
                        await self._validate_cancellation(user, order)

                    now = datetime.now(timezone.utc)
                    await conn.execute(
                        "UPDATE orders SET status = $1, updated_at = $2 WHERE id = $3",
                        target.value, now, order_id,
                    )

                    event_type = ORDER_STATUS_CHANGED
                    if target == OrderStatus.CONFIRMED:
                        event_type = ORDER_CONFIRMED
                    elif target == OrderStatus.CANCELLED:
                        event_type = ORDER_CANCELLED

                await emit_and_publish(DomainEvent(
                    event_type=event_type,
                    payload={
                        "order_id": order_id,
                        "from_status": current,
                        "to_status": target.value,
                    },
                    user_id=user.user_id,
                    restaurant_id=user.restaurant_id,
                    branch_id=user.branch_id,
                ))

                logger.info(
                    "order_status_changed",
                    order_id=order_id,
                    from_status=current,
                    to_status=target.value,
                )

                return {"id": order_id, "status": target.value}

        except LockError:
            raise LockAcquisitionError(f"order:{order_id}")

    # ── GET ORDERS (paginated, inline items, newest-first) ──

    async def get_orders(
        self,
        user: UserContext,
        status: Optional[str] = None,
        source: Optional[str] = None,
        branch_id: Optional[str] = None,
        order_type: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        include_items: bool = True,
        include_non_revenue: bool = False,
    ) -> dict:
        """
        Fetch orders with tenant isolation, newest-first, with inline items.

        Date filter semantics (IST)
        ---------------------------
        from_date  inclusive : created_at >= 00:00 IST on from_date  (UTC instant)
        to_date    inclusive : created_at <  00:00 IST on (to_date+1) (UTC instant)

        Non-revenue filter
        ------------------
        When `include_non_revenue` is False (default), orders whose `status`
        — OR whose latest `payments.status` — falls into the
        NON_REVENUE_ORDER_STATUSES / NON_REVENUE_PAYMENT_STATUSES set are
        hidden. This matches the FE filter so cancelled QRs / abandoned
        intents don't appear on the operator Orders list. Refund/cancel
        admin views should pass `include_non_revenue=True`.

        Response shape
        --------------
        {
            "items":     [...],   # order objects with nested items[]
            "page":      int,
            "page_size": int,
            "has_more":  bool,
            "total":     int
        }

        N+1 elimination
        ---------------
        Order items are aggregated in-DB via json_agg so a single SQL round-trip
        returns orders + their items.  No per-order detail fetch is needed.
        """
        try:
            from_ts, to_ts = ist_range_utc(from_date, to_date)
        except ValueError as e:
            raise ValidationError(str(e))

        clause, params = tenant_where_clause(user, "o")

        conditions = [clause]
        if status:
            params.append(status)
            conditions.append(f"o.status = ${len(params)}")
        if source:
            params.append(source)
            conditions.append(f"o.source = ${len(params)}::order_source")
        if branch_id:
            params.append(branch_id)
            conditions.append(f"o.branch_id = ${len(params)}")
        if order_type:
            # order_type maps to source column
            params.append(order_type)
            conditions.append(f"o.source = ${len(params)}::order_source")
        if from_date:
            params.append(from_ts)
            # Inclusive lower bound: created_at >= IST midnight of from_date (UTC)
            conditions.append(f"o.created_at >= ${len(params)}")
        if to_date:
            params.append(to_ts)
            # Inclusive upper bound: created_at < IST midnight of (to_date + 1 day) (UTC)
            conditions.append(f"o.created_at < ${len(params)}")

        # Hide non-revenue orders (cancelled QRs, failed/expired intents,
        # refunded, unpaid pending_payment). Mirrors the FE filter shipped
        # in release c1dc17d. Refund admin views opt-in with
        # include_non_revenue=True.
        if not include_non_revenue:
            from app.core.order_status import (
                NON_REVENUE_ORDER_STATUSES,
                NON_REVENUE_PAYMENT_STATUSES,
            )
            order_list = ",".join(f"'{s}'" for s in sorted(NON_REVENUE_ORDER_STATUSES))
            pay_list = ",".join(f"'{s}'" for s in sorted(NON_REVENUE_PAYMENT_STATUSES))
            conditions.append(
                f"LOWER(o.status) NOT IN ({order_list}) "
                f"AND NOT EXISTS ("
                f"  SELECT 1 FROM payments p_nr "
                f"   WHERE p_nr.order_id = o.id "
                f"     AND LOWER(p_nr.status) IN ({pay_list})"
                f")"
            )

        where = " AND ".join(conditions)

        # Count query uses the same WHERE clause (no LIMIT/OFFSET)
        count_params = params[:]
        total_idx = len(count_params) + 1

        # Data query parameters: append LIMIT and OFFSET
        data_params = params[:]
        data_params.append(limit)
        data_params.append(offset)
        limit_idx = len(data_params) - 1
        offset_idx = len(data_params)

        items_agg = ""
        if include_items:
            items_agg = """
            ,COALESCE(
                json_agg(
                    json_build_object(
                        'id',          oi.id,
                        'item_id',     oi.item_id,
                        'item_name',   oi.item_name,
                        'variant_id',  oi.variant_id,
                        'quantity',    oi.quantity,
                        'unit_price',  oi.unit_price,
                        'total_price', oi.total_price,
                        'addons',      oi.addons,
                        'notes',       oi.notes
                    ) ORDER BY oi.id
                ) FILTER (WHERE oi.id IS NOT NULL),
                '[]'::json
            ) AS items
            """

        async with get_connection() as conn:
            total_row = await conn.fetchrow(
                f"SELECT COUNT(*) AS cnt FROM orders o WHERE {where}",
                *count_params,
            )
            total = total_row["cnt"] if total_row else 0

            rows = await conn.fetch(
                f"""
                SELECT
                    o.id,
                    COALESCE(o.metadata->>'order_number', LEFT(o.id::text, 8)) AS display_order_number,
                    o.status,
                    o.source,
                    o.subtotal,
                    o.tax_amount,
                    o.discount_amount,
                    o.total_amount,
                    o.table_number,
                    o.delivery_address,
                    o.customer_id,
                    o.notes,
                    o.branch_id,
                    o.restaurant_id,
                    o.created_at,
                    o.updated_at,
                    c.name        AS customer_name,
                    c.phone_number AS customer_phone
                    {items_agg}
                FROM orders o
                LEFT JOIN customers c ON c.id = o.customer_id
                {"LEFT JOIN order_items oi ON oi.order_id = o.id" if include_items else ""}
                WHERE {where}
                {"GROUP BY o.id, c.name, c.phone_number" if include_items else ""}
                ORDER BY o.created_at DESC
                LIMIT ${limit_idx} OFFSET ${offset_idx}
                """,
                *data_params,
            )

        page = (offset // limit) + 1 if limit > 0 else 1
        order_list = []
        for r in rows:
            row_dict = dict(r)
            # Normalise items: json_agg returns a string in asyncpg
            if include_items:
                raw_items = row_dict.get("items")
                if isinstance(raw_items, str):
                    row_dict["items"] = json.loads(raw_items)
                elif raw_items is None:
                    row_dict["items"] = []
            # Use the display_order_number as order_number if the column doesn't exist
            if not row_dict.get("order_number"):
                row_dict["order_number"] = row_dict.get("display_order_number", "")
            row_dict.pop("display_order_number", None)
            order_list.append(row_dict)

        return {
            "items": order_list,
            "page": page,
            "page_size": limit,
            "has_more": (offset + limit) < total,
            "total": total,
        }

    async def get_order_detail(self, user: UserContext, order_id: str) -> dict:
        """Fetch order with items[]. Tenant-scoped. Returns 404 if unauthorized/missing."""
        async with get_connection() as conn:
            order = await conn.fetchrow(
                """
                SELECT o.*, c.name as customer_name, c.phone_number as customer_phone
                FROM orders o
                LEFT JOIN customers c ON c.id = o.customer_id
                WHERE o.id = $1 AND o.user_id = $2
                """,
                order_id, user.owner_id if user.is_branch_user else user.user_id,
            )
            if not order:
                raise NotFoundError("Order", order_id)

            items = await conn.fetch(
                """
                SELECT id, item_id, item_name, variant_id,
                       quantity, unit_price, total_price, addons, notes
                FROM order_items
                WHERE order_id = $1
                ORDER BY id
                """,
                order_id,
            )
            result = dict(order)
            result["items"] = [dict(i) for i in items]
            # Keep order_items alias for backward compat
            result["order_items"] = result["items"]
            # Normalise order_number
            if not result.get("order_number"):
                result["order_number"] = result.get("metadata", {}).get("order_number", order_id[:8].upper()) if isinstance(result.get("metadata"), dict) else order_id[:8].upper()
            return result

    # ── PRIVATE HELPERS ──

    async def _validate_cancellation(self, user: UserContext, order: dict):
        """Only owner/manager can cancel after confirmation."""
        status = OrderStatus(order["status"])
        if status in (OrderStatus.PREPARING, OrderStatus.READY):
            if user.role not in ("owner", "manager"):
                raise ForbiddenError("Only owner/manager can cancel orders in preparation")

    async def _calculate_coupon_discount(
        self, conn, coupon_id: int, subtotal: Decimal, customer_id: Optional[int], user_id: str
    ) -> Decimal:
        coupon = await conn.fetchrow(
            """
            SELECT * FROM coupons
            WHERE id = $1 AND user_id = $2 AND is_active = true
            AND (valid_from IS NULL OR valid_from <= now())
            AND (valid_until IS NULL OR valid_until >= now())
            """,
            coupon_id, user_id,
        )
        if not coupon:
            return Decimal("0")

        if coupon["min_order_value"] and subtotal < Decimal(str(coupon["min_order_value"])):
            return Decimal("0")

        if coupon["usage_limit"] and coupon["times_used"] >= coupon["usage_limit"]:
            return Decimal("0")

        discount_value = Decimal(str(coupon["discount_value"]))
        if coupon["type"] == "percentage":
            discount = subtotal * discount_value / 100
            max_disc = Decimal(str(coupon["max_discount"])) if coupon["max_discount"] else None
            if max_disc and max_disc > 0:
                discount = min(discount, max_disc)
        else:
            discount = discount_value

        return min(discount, subtotal)  # Never exceed subtotal

    async def _get_tax_percentage(self, conn, restaurant_id: Optional[str]) -> Decimal:
        if not restaurant_id:
            return Decimal("0")
        row = await conn.fetchrow(
            "SELECT tax_percentage FROM restaurant_settings WHERE restaurant_id = $1",
            restaurant_id,
        )
        return Decimal(str(row["tax_percentage"])) if row else Decimal("5.0")


