"""
Strict state machines for all critical entities.
Enforces valid transitions and prevents illegal state changes.
Every state change is auditable and emits domain events.
"""
from enum import Enum
from typing import Optional

from app.core.exceptions import InvalidStateTransition


# ═══════════════════════════════════════════════════════════
# ORDER STATE MACHINE
# ═══════════════════════════════════════════════════════════

class OrderStatus(str, Enum):
    QUEUED = "Queued"          # Dine-in / QR table
    PENDING = "Pending"
    CONFIRMED = "Confirmed"
    PREPARING = "Preparing"
    READY = "Ready"
    SERVED = "Served"          # Dine-in / QR table
    OUT_FOR_DELIVERY = "Out for Delivery"
    DELIVERED = "Delivered"
    CANCELLED = "Cancelled"
    REJECTED = "Rejected"

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderStatus.SERVED,
            OrderStatus.DELIVERED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        )


ORDER_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    # ── Delivery flow ──
    OrderStatus.PENDING: {
        OrderStatus.CONFIRMED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    },
    OrderStatus.CONFIRMED: {
        OrderStatus.PREPARING,
        OrderStatus.CANCELLED,
    },
    # ── Dine-in / QR table flow ──
    OrderStatus.QUEUED: {
        OrderStatus.PREPARING,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    },
    # ── Shared ──
    OrderStatus.PREPARING: {
        OrderStatus.READY,
        OrderStatus.CANCELLED,  # Only within grace period
    },
    OrderStatus.READY: {
        OrderStatus.SERVED,            # Dine-in / takeaway
        OrderStatus.OUT_FOR_DELIVERY,  # Delivery
        OrderStatus.DELIVERED,         # Legacy / direct delivery
    },
    OrderStatus.SERVED: set(),         # Terminal for dine-in
    OrderStatus.OUT_FOR_DELIVERY: {
        OrderStatus.DELIVERED,
    },
    # Terminal states — no further transitions
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
}


def validate_order_transition(current: str, target: str) -> OrderStatus:
    """Validate and return the target OrderStatus if transition is legal.
    Returns None if current == target (idempotent no-op)."""
    try:
        current_status = OrderStatus(current)
        target_status = OrderStatus(target)
    except ValueError as e:
        raise InvalidStateTransition("order", current, target) from e

    # Idempotent: same status is a no-op
    if current_status == target_status:
        return None

    if target_status not in ORDER_TRANSITIONS.get(current_status, set()):
        raise InvalidStateTransition("order", current, target)

    return target_status


# ═══════════════════════════════════════════════════════════
# PAYMENT STATE MACHINE
# ═══════════════════════════════════════════════════════════

class PaymentStatus(str, Enum):
    INITIATED = "initiated"       # Payment intent created
    PENDING = "pending"            # Sent to gateway, awaiting response
    COMPLETED = "completed"        # Payment successful (a.k.a. "success")
    FAILED = "failed"              # Gateway rejected / timed out
    SETTLED = "settled"            # Funds settled by gateway into bank
    REFUNDED = "refunded"          # Fully refunded
    RECONCILED = "reconciled"      # Matched in bank statement


PAYMENT_TRANSITIONS: dict[PaymentStatus, set[PaymentStatus]] = {
    PaymentStatus.INITIATED: {
        PaymentStatus.PENDING,
        PaymentStatus.FAILED,    # Instant rejection
    },
    PaymentStatus.PENDING: {
        PaymentStatus.COMPLETED,
        PaymentStatus.FAILED,
    },
    PaymentStatus.COMPLETED: {
        PaymentStatus.SETTLED,
        PaymentStatus.REFUNDED,
    },
    PaymentStatus.FAILED: {
        PaymentStatus.PENDING,   # Retry
    },
    PaymentStatus.SETTLED: {
        PaymentStatus.REFUNDED,
        PaymentStatus.RECONCILED,
    },
    PaymentStatus.REFUNDED: set(),      # Terminal
    PaymentStatus.RECONCILED: set(),    # Terminal
}


def validate_payment_transition(current: str, target: str) -> PaymentStatus:
    try:
        current_status = PaymentStatus(current)
        target_status = PaymentStatus(target)
    except ValueError as e:
        raise InvalidStateTransition("payment", current, target) from e

    if target_status not in PAYMENT_TRANSITIONS.get(current_status, set()):
        raise InvalidStateTransition("payment", current, target)

    return target_status


# ═══════════════════════════════════════════════════════════
# KITCHEN ORDER STATE MACHINE
# ═══════════════════════════════════════════════════════════

class KitchenStatus(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    READY = "ready"
    SERVED = "served"


KITCHEN_TRANSITIONS: dict[KitchenStatus, set[KitchenStatus]] = {
    KitchenStatus.QUEUED: {KitchenStatus.PREPARING},
    KitchenStatus.PREPARING: {KitchenStatus.READY},
    KitchenStatus.READY: {KitchenStatus.SERVED},
    KitchenStatus.SERVED: set(),  # Terminal
}


def validate_kitchen_transition(current: str, target: str) -> KitchenStatus:
    try:
        current_status = KitchenStatus(current)
        target_status = KitchenStatus(target)
    except ValueError as e:
        raise InvalidStateTransition("kitchen_order", current, target) from e

    if target_status not in KITCHEN_TRANSITIONS.get(current_status, set()):
        raise InvalidStateTransition("kitchen_order", current, target)

    return target_status


# ═══════════════════════════════════════════════════════════
# DELIVERY STATE MACHINE
# ═══════════════════════════════════════════════════════════

class DeliveryStatus(str, Enum):
    UNASSIGNED = "unassigned"
    ASSIGNED = "assigned"
    PICKED_UP = "picked_up"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    FAILED = "failed"


DELIVERY_TRANSITIONS: dict[DeliveryStatus, set[DeliveryStatus]] = {
    DeliveryStatus.UNASSIGNED: {DeliveryStatus.ASSIGNED, DeliveryStatus.FAILED},
    DeliveryStatus.ASSIGNED: {
        DeliveryStatus.PICKED_UP,
        DeliveryStatus.UNASSIGNED,  # Reassignment
        DeliveryStatus.FAILED,
    },
    DeliveryStatus.PICKED_UP: {DeliveryStatus.OUT_FOR_DELIVERY, DeliveryStatus.FAILED},
    DeliveryStatus.OUT_FOR_DELIVERY: {DeliveryStatus.DELIVERED, DeliveryStatus.FAILED},
    DeliveryStatus.DELIVERED: set(),
    DeliveryStatus.FAILED: {DeliveryStatus.UNASSIGNED},  # Retry with new partner
}


def validate_delivery_transition(current: str, target: str) -> DeliveryStatus:
    try:
        current_status = DeliveryStatus(current)
        target_status = DeliveryStatus(target)
    except ValueError as e:
        raise InvalidStateTransition("delivery", current, target) from e

    if target_status not in DELIVERY_TRANSITIONS.get(current_status, set()):
        raise InvalidStateTransition("delivery", current, target)

    return target_status


# ═══════════════════════════════════════════════════════════
# TABLE SESSION STATE MACHINE
# ═══════════════════════════════════════════════════════════

class TableStatus(str, Enum):
    BLANK = "blank"
    RUNNING = "running"
    RUNNING_KOT = "running_kot"
    PRINTED = "printed"
    PAID = "paid"


TABLE_TRANSITIONS: dict[TableStatus, set[TableStatus]] = {
    TableStatus.BLANK: {TableStatus.RUNNING},
    TableStatus.RUNNING: {TableStatus.RUNNING_KOT, TableStatus.PRINTED, TableStatus.BLANK},
    TableStatus.RUNNING_KOT: {TableStatus.PRINTED, TableStatus.RUNNING},
    TableStatus.PRINTED: {TableStatus.PAID, TableStatus.RUNNING},
    TableStatus.PAID: {TableStatus.BLANK},
}


def validate_table_transition(current: str, target: str) -> TableStatus:
    try:
        current_status = TableStatus(current)
        target_status = TableStatus(target)
    except ValueError as e:
        raise InvalidStateTransition("table", current, target) from e

    if target_status not in TABLE_TRANSITIONS.get(current_status, set()):
        raise InvalidStateTransition("table", current, target)

    return target_status


# ═══════════════════════════════════════════════════════════
# SUBSCRIPTION STATE MACHINE
# ═══════════════════════════════════════════════════════════

class SubscriptionStatus(str, Enum):
    TRIAL = "TRIAL"
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    GRACE_PERIOD = "GRACE_PERIOD"
    SUSPENDED = "SUSPENDED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    PAYMENT_PENDING = "PAYMENT_PENDING"


SUBSCRIPTION_TRANSITIONS: dict[SubscriptionStatus, set[SubscriptionStatus]] = {
    SubscriptionStatus.TRIAL: {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.EXPIRED,
        SubscriptionStatus.PAYMENT_PENDING,
    },
    SubscriptionStatus.PAYMENT_PENDING: {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.EXPIRED,
    },
    SubscriptionStatus.ACTIVE: {
        SubscriptionStatus.PAST_DUE,
        SubscriptionStatus.CANCELLED,
    },
    SubscriptionStatus.PAST_DUE: {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.GRACE_PERIOD,
    },
    SubscriptionStatus.GRACE_PERIOD: {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.SUSPENDED,
    },
    SubscriptionStatus.SUSPENDED: {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.CANCELLED,
    },
    SubscriptionStatus.CANCELLED: {SubscriptionStatus.ACTIVE},  # Reactivation
    SubscriptionStatus.EXPIRED: {SubscriptionStatus.ACTIVE},  # Re-subscribe
}
