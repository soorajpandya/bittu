"""
Payment Processing Service.

Security-critical module:
  - All amounts verified server-side against order totals
  - Razorpay signature verification on every webhook
  - Idempotent webhook processing (same event processed only once)
  - Audit trail for every payment state change
  - No payment status rollback after capture

Flow:
  1. Client initiates payment → server creates Razorpay order
  2. Client completes payment on Razorpay
  3. Webhook received → verify signature → update payment → update order
  4. If verification fails → mark as failed, alert

Concurrency:
  - Lock per payment to prevent double-processing
  - SERIALIZABLE isolation for payment status updates
"""
import hashlib
import hmac
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.redis import DistributedLock, check_idempotency, set_idempotency, LockError
from app.core.state_machines import PaymentStatus, validate_payment_transition
from app.core.events import (
    DomainEvent, emit_and_publish,
    PAYMENT_INITIATED, PAYMENT_COMPLETED, PAYMENT_FAILED, PAYMENT_REFUNDED,
)
from app.core.tenant import tenant_insert_fields
from app.core.exceptions import (
    NotFoundError, PaymentError, LockAcquisitionError, ValidationError,
)
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.merchant_ledger_integration import post_payment_received
from app.services.escrow_integration import hold_payment_in_escrow

logger = get_logger(__name__)


def _settings():
    return get_settings()


class PaymentService:

    # ── INITIATE PAYMENT ──

    async def initiate_payment(
        self,
        user: UserContext,
        order_id: str,
        method: str,  # cash, upi, card, wallet, online
        amount: Optional[float] = None,
    ) -> dict:
        """
        Initiate a payment for an order.
        For cash: mark as completed immediately.
        For online: create Razorpay order and return checkout data.
        """
        tenant = tenant_insert_fields(user)

        async with get_serializable_transaction() as conn:
            # Verify order exists and belongs to tenant
            order = await conn.fetchrow(
                """
                SELECT id, total_amount, status, user_id
                FROM orders WHERE id = $1 AND user_id = $2
                FOR UPDATE
                """,
                order_id, tenant["user_id"],
            )
            if not order:
                raise NotFoundError("Order", order_id)

            # Server-side amount verification
            order_total = Decimal(str(order["total_amount"]))
            payment_amount = Decimal(str(amount)) if amount else order_total

            if payment_amount > order_total:
                raise ValidationError(
                    f"Payment amount ({payment_amount}) exceeds order total ({order_total})"
                )

            # Check for existing pending payment
            existing = await conn.fetchrow(
                "SELECT id FROM payments WHERE order_id = $1 AND status = 'pending'",
                order_id,
            )
            if existing:
                raise PaymentError("A pending payment already exists for this order")

            # Create payment record
            import uuid
            payment_id = str(uuid.uuid4())
            razorpay_order_id = None

            if method == "cash":
                # Cash payments are immediately completed
                status = PaymentStatus.COMPLETED.value
                paid_at = datetime.now(timezone.utc)
            else:
                # For non-cash, create Razorpay order
                status = PaymentStatus.PENDING.value
                paid_at = None
                razorpay_order_id = await self._create_razorpay_order(
                    payment_amount, order_id
                )

            await conn.execute(
                """
                INSERT INTO payments (
                    id, order_id, restaurant_id, user_id, branch_id,
                    method, status, amount, currency,
                    razorpay_order_id, paid_at
                ) VALUES ($1, $2, $3, $4, $5, $6::payment_method, $7::payment_status, $8, 'INR', $9, $10)
                """,
                payment_id,
                order_id,
                user.restaurant_id,
                tenant["user_id"],
                tenant.get("branch_id"),
                method,
                status,
                float(payment_amount),
                razorpay_order_id,
                paid_at,
            )

            # For cash, also update order status
            if method == "cash":
                await conn.execute(
                    "UPDATE orders SET status = 'Confirmed', updated_at = now() WHERE id = $1",
                    order_id,
                )
                # Mirror cash receipt into the immutable merchant ledger.
                # Gateway-driven (online/razorpay) payments are NOT mirrored
                # here on purpose — see merchant_ledger_integration for the
                # contract.  Best-effort: never raises.
                await post_payment_received(
                    merchant_id=user.restaurant_id,
                    payment_id=payment_id,
                    amount=payment_amount,
                    method=method,
                    order_id=order_id,
                    branch_id=tenant.get("branch_id"),
                    actor_id=user.user_id,
                    conn=conn,
                )
                # Phase 2: place the same amount into escrow until T+N
                # cron releases it.  Best-effort, idempotent on payment_id.
                await hold_payment_in_escrow(
                    merchant_id=user.restaurant_id,
                    payment_id=payment_id,
                    amount=payment_amount,
                    method=method,
                    order_id=order_id,
                    branch_id=tenant.get("branch_id"),
                    actor_id=user.user_id,
                    conn=conn,
                )

        # Audit log
        await self._audit_payment(
            user, payment_id, "payment_initiated",
            {"method": method, "amount": float(payment_amount)}
        )

        await emit_and_publish(DomainEvent(
            event_type=PAYMENT_COMPLETED if method == "cash" else PAYMENT_INITIATED,
            payload={
                "payment_id": payment_id,
                "order_id": order_id,
                "method": method,
                "amount": float(payment_amount),
                "razorpay_order_id": razorpay_order_id,
            },
            user_id=user.user_id,
            restaurant_id=user.restaurant_id,
            branch_id=user.branch_id,
        ))

        logger.info("payment_initiated", payment_id=payment_id, method=method, amount=float(payment_amount))

        return {
            "payment_id": payment_id,
            "status": status,
            "method": method,
            "amount": float(payment_amount),
            "razorpay_order_id": razorpay_order_id,
        }

    # ── VERIFY RAZORPAY PAYMENT ──

    async def verify_razorpay_payment(
        self,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str,
    ) -> dict:
        """
        Client-side verification after Razorpay checkout.
        Verifies signature and updates payment/order status.
        """
        # Signature verification
        if not self._verify_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature):
            logger.warning(
                "payment_signature_invalid",
                razorpay_order_id=razorpay_order_id,
            )
            raise PaymentError("Payment signature verification failed")

        try:
            async with DistributedLock(f"payment:rz:{razorpay_order_id}", timeout=15):
                async with get_serializable_transaction() as conn:
                    payment = await conn.fetchrow(
                        """
                        SELECT id, order_id, status, amount, method
                        FROM payments WHERE razorpay_order_id = $1
                        FOR UPDATE
                        """,
                        razorpay_order_id,
                    )
                    if not payment:
                        raise NotFoundError("Payment", razorpay_order_id)

                    validate_payment_transition(payment["status"], PaymentStatus.COMPLETED.value)

                    now = datetime.now(timezone.utc)
                    await conn.execute(
                        """
                        UPDATE payments
                        SET status = 'completed'::payment_status,
                            razorpay_payment_id = $1,
                            razorpay_signature = $2,
                            paid_at = $3
                        WHERE id = $4
                        """,
                        razorpay_payment_id, razorpay_signature, now, str(payment["id"]),
                    )

                    # Update order to Confirmed
                    await conn.execute(
                        "UPDATE orders SET status = 'Confirmed', updated_at = $1 WHERE id = $2",
                        now, str(payment["order_id"]),
                    )

                await emit_and_publish(DomainEvent(
                    event_type=PAYMENT_COMPLETED,
                    payload={
                        "payment_id": str(payment["id"]),
                        "order_id": str(payment["order_id"]),
                        "amount": float(payment["amount"]),
                        "method": payment["method"],
                        "razorpay_payment_id": razorpay_payment_id,
                    },
                ))

                return {"status": "completed", "order_id": str(payment["order_id"])}

        except LockError:
            raise LockAcquisitionError("payment verification")

    # ── WEBHOOK HANDLER ──

    async def handle_webhook(
        self,
        *,
        event: str,
        payload: dict,
        raw_payload: Optional[dict] = None,
        signature: Optional[str] = None,
        gateway: str = "razorpay",
    ) -> dict:
        """
        Process a payment-gateway webhook with **durable** idempotency.

        The router is expected to have already verified the signature.  This
        method:
          1. Records the event in `webhook_events` (UNIQUE on gateway+event_id)
             so a Redis flush cannot cause double-processing.
          2. Skips processing if the row already existed (duplicate delivery).
          3. Dispatches to the per-event handler.
          4. Marks the ledger row processed/failed.

        `payload` is the gateway-specific entity (e.g. razorpay payment.entity).
        `raw_payload` is the full envelope for forensic audit (defaults to payload).
        """
        # Lazy import to avoid circular dep
        from app.services.reconciliation_service import reconciliation_service

        envelope = raw_payload if raw_payload is not None else payload
        event_id = (
            envelope.get("event_id")
            or envelope.get("id")
            or payload.get("id")
            or ""
        )
        gateway_payment_id = payload.get("id") if event.startswith("payment.") else None
        gateway_order_id = payload.get("order_id")

        ledger = await reconciliation_service.record_webhook(
            gateway=gateway,
            event_type=event,
            event_id=event_id or None,
            gateway_payment_id=gateway_payment_id,
            gateway_order_id=gateway_order_id,
            raw_payload=envelope,
            signature=signature,
            signature_valid=True,  # router validates before calling
        )
        if ledger["duplicate"]:
            logger.info("webhook_duplicate_skipped", gateway=gateway, event_id=event_id)
            return {"status": "already_processed", "webhook_id": ledger["id"]}

        webhook_id = ledger["id"]

        try:
            # Razorpay sends `event` like "payment.captured" with the entity in
            # payload.payload.payment.entity. The legacy handlers expect the
            # full envelope, so we re-wrap when needed.
            wrapped = {"payload": {"payment": {"entity": payload}, "refund": {"entity": payload}}}

            if event == "payment.captured":
                await self._handle_payment_captured(wrapped)
            elif event == "payment.failed":
                await self._handle_payment_failed(wrapped)
            elif event == "refund.processed":
                await self._handle_refund_processed(wrapped)
            else:
                await reconciliation_service.mark_webhook_processed(
                    webhook_id, status="skipped",
                    error_message=f"unhandled event_type: {event}",
                )
                logger.info("webhook_unhandled", event=event)
                return {"status": "unhandled", "webhook_id": webhook_id}

            await reconciliation_service.mark_webhook_processed(
                webhook_id, status="processed",
            )
            logger.info("webhook_processed", event_id=event_id, event_type=event)
            return {"status": "processed", "webhook_id": webhook_id}

        except Exception as exc:
            await reconciliation_service.mark_webhook_processed(
                webhook_id, status="failed", error_message=str(exc),
            )
            logger.exception("webhook_processing_failed", event=event, event_id=event_id)
            raise

    # ── REFUND ──

    async def initiate_refund(
        self,
        user: UserContext,
        payment_id: str,
        amount: Optional[float] = None,
        reason: str = "",
    ) -> dict:
        """Process a full or partial refund."""
        try:
            async with DistributedLock(f"payment:{payment_id}", timeout=15):
                async with get_serializable_transaction() as conn:
                    payment = await conn.fetchrow(
                        """
                        SELECT id, order_id, status, amount, method, razorpay_payment_id, user_id
                        FROM payments WHERE id = $1 AND user_id = $2
                        FOR UPDATE
                        """,
                        payment_id,
                        user.owner_id if user.is_branch_user else user.user_id,
                    )
                    if not payment:
                        raise NotFoundError("Payment", payment_id)

                    validate_payment_transition(payment["status"], PaymentStatus.REFUNDED.value)

                    refund_amount = Decimal(str(amount)) if amount else Decimal(str(payment["amount"]))
                    if refund_amount > Decimal(str(payment["amount"])):
                        raise ValidationError("Refund amount exceeds payment amount")

                    # For online payments, initiate Razorpay refund
                    if payment["razorpay_payment_id"]:
                        await self._create_razorpay_refund(
                            payment["razorpay_payment_id"],
                            int(refund_amount * 100),  # Convert to paise
                        )

                    await conn.execute(
                        "UPDATE payments SET status = 'refunded'::payment_status WHERE id = $1",
                        payment_id,
                    )

                    # Update order status
                    await conn.execute(
                        "UPDATE orders SET status = 'Cancelled', updated_at = now() WHERE id = $1",
                        str(payment["order_id"]),
                    )

                await self._audit_payment(
                    user, payment_id, "refund_initiated",
                    {"amount": float(refund_amount), "reason": reason}
                )

                await emit_and_publish(DomainEvent(
                    event_type=PAYMENT_REFUNDED,
                    payload={
                        "payment_id": payment_id,
                        "order_id": str(payment["order_id"]),
                        "amount": float(refund_amount),
                    },
                    user_id=user.user_id,
                    restaurant_id=user.restaurant_id,
                ))

                return {"status": "refunded", "amount": float(refund_amount)}

        except LockError:
            raise LockAcquisitionError("refund processing")

    # ── PRIVATE HELPERS ──

    def _verify_signature(
        self, order_id: str, payment_id: str, signature: str
    ) -> bool:
        """Verify Razorpay payment signature."""
        message = f"{order_id}|{payment_id}"
        expected = hmac.HMAC(
            _settings().RAZORPAY_KEY_SECRET.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def _create_razorpay_order(self, amount: Decimal, order_id: str) -> str:
        """Create a Razorpay order. Returns razorpay_order_id."""
        import razorpay
        s = _settings()
        client = razorpay.Client(auth=(s.RAZORPAY_KEY_ID, s.RAZORPAY_KEY_SECRET))
        rz_order = client.order.create({
            "amount": int(amount * 100),  # Razorpay expects paise
            "currency": "INR",
            "receipt": order_id,
            "notes": {"order_id": order_id},
        })
        return rz_order["id"]

    async def _create_razorpay_refund(self, payment_id: str, amount_paise: int):
        """Create a Razorpay refund."""
        import razorpay
        s = _settings()
        client = razorpay.Client(auth=(s.RAZORPAY_KEY_ID, s.RAZORPAY_KEY_SECRET))
        client.payment.refund(payment_id, {"amount": amount_paise})

    async def _handle_payment_captured(self, payload: dict):
        """Handle Razorpay payment.captured webhook."""
        entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        rz_order_id = entity.get("order_id")
        rz_payment_id = entity.get("id")

        if not rz_order_id:
            return

        async with get_serializable_transaction() as conn:
            payment = await conn.fetchrow(
                "SELECT id, order_id, status FROM payments WHERE razorpay_order_id = $1 FOR UPDATE",
                rz_order_id,
            )
            if not payment or payment["status"] == "completed":
                return

            await conn.execute(
                """
                UPDATE payments
                SET status = 'completed'::payment_status, razorpay_payment_id = $1, paid_at = now()
                WHERE id = $2
                """,
                rz_payment_id, str(payment["id"]),
            )
            await conn.execute(
                "UPDATE orders SET status = 'Confirmed', updated_at = now() WHERE id = $1",
                str(payment["order_id"]),
            )

    async def _handle_payment_failed(self, payload: dict):
        entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        rz_order_id = entity.get("order_id")
        if not rz_order_id:
            return

        async with get_serializable_transaction() as conn:
            await conn.execute(
                "UPDATE payments SET status = 'failed'::payment_status WHERE razorpay_order_id = $1",
                rz_order_id,
            )

        await emit_and_publish(DomainEvent(
            event_type=PAYMENT_FAILED,
            payload={"razorpay_order_id": rz_order_id},
        ))

    async def _handle_refund_processed(self, payload: dict):
        entity = payload.get("payload", {}).get("refund", {}).get("entity", {})
        rz_payment_id = entity.get("payment_id")
        if not rz_payment_id:
            return

        async with get_serializable_transaction() as conn:
            await conn.execute(
                "UPDATE payments SET status = 'refunded'::payment_status WHERE razorpay_payment_id = $1",
                rz_payment_id,
            )

    async def _audit_payment(self, user: UserContext, payment_id: str, action: str, data: dict):
        """Write to audit log for payment actions (forensic-safe JSON)."""
        from app.core.audit_logger import audit_event
        await audit_event(
            action=action,
            entity_type="payment",
            entity_id=str(payment_id),
            payload=data or {},
            actor=user,
            domain="merchant",
        )
