"""
Razorpay webhook router (Phase 3 fills handler dispatch).

Phase 1 surface: re-exports `verify_webhook_signature` and declares the
event-type constants so `app.api.v1.webhooks` and the recon engine share
a single source of truth.

The actual `/webhooks/razorpay/payment` HTTP route lives in
`app/api/v1/webhooks.py` and is rewired in Phase 3 to call into this module.

NOTE: Event names below match the Razorpay dashboard exactly. Anything
outside `HANDLED_EVENTS` is silently acknowledged (200 OK) so Razorpay
stops retrying — Phase 3 logs them at INFO level.

Out of scope (do NOT subscribe on the dashboard):
  * subscription.*           — no subscription product
  * payment_link.*           — using orders + QR instead
  * fund_account.*           — RazorpayX, out of scope
  * account.*                — partner-mode, out of scope
"""
from __future__ import annotations

from app.services.razorpay.payments import verify_webhook_signature

# ── Payments ──────────────────────────────────────────────────────────────
EVENT_PAYMENT_AUTHORIZED       = "payment.authorized"
EVENT_PAYMENT_CAPTURED         = "payment.captured"
EVENT_PAYMENT_FAILED           = "payment.failed"

# ── Disputes (note the `payment.` prefix — Razorpay's real names) ─────────
EVENT_DISPUTE_CREATED          = "payment.dispute.created"
EVENT_DISPUTE_WON              = "payment.dispute.won"
EVENT_DISPUTE_LOST             = "payment.dispute.lost"
EVENT_DISPUTE_CLOSED           = "payment.dispute.closed"
EVENT_DISPUTE_UNDER_REVIEW     = "payment.dispute.under_review"
EVENT_DISPUTE_ACTION_REQUIRED  = "payment.dispute.action_required"

# ── Downtime (operational signal — surface to ops dashboard) ──────────────
EVENT_DOWNTIME_STARTED         = "payment.downtime.started"
EVENT_DOWNTIME_UPDATED         = "payment.downtime.updated"
EVENT_DOWNTIME_RESOLVED        = "payment.downtime.resolved"

# ── Orders ────────────────────────────────────────────────────────────────
EVENT_ORDER_PAID               = "order.paid"

# ── Invoices ──────────────────────────────────────────────────────────────
EVENT_INVOICE_PAID             = "invoice.paid"
EVENT_INVOICE_PARTIALLY_PAID   = "invoice.partially_paid"
EVENT_INVOICE_EXPIRED          = "invoice.expired"

# ── Settlements ───────────────────────────────────────────────────────────
EVENT_SETTLEMENT_PROCESSED     = "settlement.processed"

# ── Virtual Accounts (Smart Collect) ──────────────────────────────────────
EVENT_VA_CREATED               = "virtual_account.created"
EVENT_VA_CREDITED              = "virtual_account.credited"
EVENT_VA_CLOSED                = "virtual_account.closed"

# ── QR Codes ──────────────────────────────────────────────────────────────
EVENT_QR_CODE_CREATED          = "qr_code.created"
EVENT_QR_CODE_CREDITED         = "qr_code.credited"
EVENT_QR_CODE_CLOSED           = "qr_code.closed"

# ── Refunds ───────────────────────────────────────────────────────────────
EVENT_REFUND_CREATED           = "refund.created"
EVENT_REFUND_PROCESSED         = "refund.processed"
EVENT_REFUND_FAILED            = "refund.failed"
EVENT_REFUND_SPEED_CHANGED     = "refund.speed_changed"

# ── Route transfers ───────────────────────────────────────────────────────
EVENT_TRANSFER_PROCESSED       = "transfer.processed"
EVENT_TRANSFER_FAILED          = "transfer.failed"


HANDLED_EVENTS: frozenset[str] = frozenset({
    # payments
    EVENT_PAYMENT_AUTHORIZED,
    EVENT_PAYMENT_CAPTURED,
    EVENT_PAYMENT_FAILED,
    # disputes
    EVENT_DISPUTE_CREATED,
    EVENT_DISPUTE_WON,
    EVENT_DISPUTE_LOST,
    EVENT_DISPUTE_CLOSED,
    EVENT_DISPUTE_UNDER_REVIEW,
    EVENT_DISPUTE_ACTION_REQUIRED,
    # downtime
    EVENT_DOWNTIME_STARTED,
    EVENT_DOWNTIME_UPDATED,
    EVENT_DOWNTIME_RESOLVED,
    # orders
    EVENT_ORDER_PAID,
    # invoices
    EVENT_INVOICE_PAID,
    EVENT_INVOICE_PARTIALLY_PAID,
    EVENT_INVOICE_EXPIRED,
    # settlements
    EVENT_SETTLEMENT_PROCESSED,
    # virtual accounts
    EVENT_VA_CREATED,
    EVENT_VA_CREDITED,
    EVENT_VA_CLOSED,
    # QR
    EVENT_QR_CODE_CREATED,
    EVENT_QR_CODE_CREDITED,
    EVENT_QR_CODE_CLOSED,
    # refunds
    EVENT_REFUND_CREATED,
    EVENT_REFUND_PROCESSED,
    EVENT_REFUND_FAILED,
    EVENT_REFUND_SPEED_CHANGED,
    # route transfers
    EVENT_TRANSFER_PROCESSED,
    EVENT_TRANSFER_FAILED,
})

__all__ = [
    "verify_webhook_signature",
    "HANDLED_EVENTS",
    "EVENT_PAYMENT_AUTHORIZED",
    "EVENT_PAYMENT_CAPTURED",
    "EVENT_PAYMENT_FAILED",
    "EVENT_DISPUTE_CREATED",
    "EVENT_DISPUTE_WON",
    "EVENT_DISPUTE_LOST",
    "EVENT_DISPUTE_CLOSED",
    "EVENT_DISPUTE_UNDER_REVIEW",
    "EVENT_DISPUTE_ACTION_REQUIRED",
    "EVENT_DOWNTIME_STARTED",
    "EVENT_DOWNTIME_UPDATED",
    "EVENT_DOWNTIME_RESOLVED",
    "EVENT_ORDER_PAID",
    "EVENT_INVOICE_PAID",
    "EVENT_INVOICE_PARTIALLY_PAID",
    "EVENT_INVOICE_EXPIRED",
    "EVENT_SETTLEMENT_PROCESSED",
    "EVENT_VA_CREATED",
    "EVENT_VA_CREDITED",
    "EVENT_VA_CLOSED",
    "EVENT_QR_CODE_CREATED",
    "EVENT_QR_CODE_CREDITED",
    "EVENT_QR_CODE_CLOSED",
    "EVENT_REFUND_CREATED",
    "EVENT_REFUND_PROCESSED",
    "EVENT_REFUND_FAILED",
    "EVENT_REFUND_SPEED_CHANGED",
    "EVENT_TRANSFER_PROCESSED",
    "EVENT_TRANSFER_FAILED",
]
