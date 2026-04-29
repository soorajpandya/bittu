"""
Dine-In Session API — QR-based ordering with session isolation.

All /qr/* endpoints are PUBLIC (no JWT) — they use session_token for auth.
All /admin/* endpoints require staff JWT.
"""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, get_current_user, require_permission
from app.core.database import get_connection
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.services.dinein_session_service import DineInSessionService

router = APIRouter(prefix="/dinein", tags=["Dine-In Sessions"])
_svc = DineInSessionService()
logger = get_logger(__name__)


# ── Pydantic schemas ────────────────────────────────────────

class QRScanIn(BaseModel):
    restaurant_id: str
    table_id: str
    device_id: str
    session_token: Optional[str] = None  # Client sends existing token for restore


class CartAddIn(BaseModel):
    session_token: str
    item_id: int
    quantity: int = Field(ge=1, default=1)
    variant_id: Optional[str] = None
    addons: Optional[list] = None
    extras: Optional[list] = None
    notes: Optional[str] = None
    device_id: Optional[str] = None
    request_id: Optional[str] = None


class CartUpdateIn(BaseModel):
    session_token: str
    cart_item_id: str
    quantity: Optional[int] = None
    addons: Optional[list] = None
    extras: Optional[list] = None
    request_id: Optional[str] = None


class CartRemoveIn(BaseModel):
    session_token: str
    cart_item_id: str


class PlaceOrderIn(BaseModel):
    session_token: str
    device_id: Optional[str] = None
    notes: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    payment_method: str = "cash"
    request_id: Optional[str] = None


class MergeSessionsIn(BaseModel):
    source_session_token: str
    target_session_token: str


class CallWaiterIn(BaseModel):
    session_token: str
    request_type: str = "assistance"  # assistance | bill | water


class CloseSessionIn(BaseModel):
    session_token: str
    reason: str = "completed"


class SplitBillIn(BaseModel):
    split_type: str = "equal"  # equal | by_item | by_user
    parts: int = 1
    item_splits: Optional[list[dict]] = None
    user_splits: Optional[list[dict]] = None


class SessionPaymentIn(BaseModel):
    amount: float = Field(gt=0)
    payment_method: str
    transaction_ref: Optional[str] = None
    paid_by: Optional[str] = None
    notes: Optional[str] = None


# ══════════════════════════════════════════════════════════════
# PUBLIC ENDPOINTS (customer-facing, no JWT)
# ══════════════════════════════════════════════════════════════

@router.post("/qr/scan")
async def qr_scan(body: QRScanIn):
    """Customer scans QR. Creates new session or restores existing one."""
    return await _svc.scan_qr(
        restaurant_id=body.restaurant_id,
        table_id=body.table_id,
        device_id=body.device_id,
        client_session_token=body.session_token,
    )


@router.get("/qr/session")
async def get_session_state(session_token: str = Query(...)):
    """Get full session state (order, cart, linked sessions) for reconnect."""
    return await _svc.get_session_state(session_token)


@router.get("/qr/menu")
async def qr_menu(restaurant_id: str = Query(...)):
    """Full menu for QR ordering."""
    return await _svc.get_menu(restaurant_id)


# ── Cart ──

@router.post("/qr/cart/add")
async def cart_add(body: CartAddIn):
    """Add item to session cart. Idempotent via request_id."""
    return await _svc.add_to_cart(
        session_token=body.session_token,
        item_id=body.item_id,
        quantity=body.quantity,
        variant_id=body.variant_id,
        addons=body.addons,
        extras=body.extras,
        notes=body.notes,
        device_id=body.device_id,
        request_id=body.request_id,
    )


@router.post("/qr/cart/update")
async def cart_update(body: CartUpdateIn):
    """Update cart item quantity or customizations."""
    return await _svc.update_cart_item(
        session_token=body.session_token,
        cart_item_id=body.cart_item_id,
        quantity=body.quantity,
        addons=body.addons,
        extras=body.extras,
        request_id=body.request_id,
    )


@router.post("/qr/cart/remove")
async def cart_remove(body: CartRemoveIn):
    """Remove item from cart."""
    return await _svc.remove_cart_item(
        session_token=body.session_token,
        cart_item_id=body.cart_item_id,
    )


@router.post("/qr/cart/clear")
async def cart_clear(session_token: str = Query(...)):
    """Clear all cart items."""
    return await _svc.clear_cart(session_token)


@router.get("/qr/cart")
async def get_cart(session_token: str = Query(...)):
    """Get cart contents for a session."""
    return await _svc.get_cart(session_token)


# ── Order ──

@router.post("/qr/place-order")
async def place_order(body: PlaceOrderIn):
    """Place order from cart. Appends to existing active order if present."""
    return await _svc.place_order(
        session_token=body.session_token,
        device_id=body.device_id,
        notes=body.notes,
        customer_name=body.customer_name,
        customer_phone=body.customer_phone,
        payment_method=body.payment_method,
        request_id=body.request_id,
    )


@router.get("/qr/order-status")
async def order_status(session_token: str = Query(...)):
    """Get orders for THIS session only (strict isolation)."""
    return await _svc.get_order_status(session_token)


# ── Merge ──

@router.post("/qr/merge")
async def merge_sessions(body: MergeSessionsIn):
    """Merge two sessions on the same table. Both see shared order post-merge."""
    return await _svc.merge_sessions(
        source_session_token=body.source_session_token,
        target_session_token=body.target_session_token,
    )


# ── Waiter / Session ──

@router.post("/qr/call-waiter")
async def call_waiter(body: CallWaiterIn):
    """Customer requests waiter assistance."""
    return await _svc.call_waiter(
        session_token=body.session_token,
        request_type=body.request_type,
    )


@router.post("/qr/close-session")
async def close_session(body: CloseSessionIn):
    """Customer closes their session."""
    return await _svc.close_session(
        session_token=body.session_token,
        reason=body.reason,
    )


# ══════════════════════════════════════════════════════════════
# ADMIN / KITCHEN ENDPOINTS (JWT required)
# ══════════════════════════════════════════════════════════════

@router.get("/admin/kitchen-view")
async def kitchen_table_view(
    user: UserContext = Depends(get_current_user),
):
    """Kitchen display: orders grouped by table, all sessions visible."""
    owner_id = user.owner_id if user.is_branch_user else user.user_id
    return await _svc.get_kitchen_table_view(
        user_id=owner_id,
        restaurant_id=user.restaurant_id or "",
    )


@router.get("/sessions/{session_id}/bill")
async def get_session_bill(
    session_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Get complete bill snapshot for a table session."""
    owner_id = user.owner_id if user.is_branch_user else user.user_id

    # Compatibility: some clients pass table_sessions.id here.
    resolved_session_id = session_id
    async with get_connection() as conn:
        dinein = await conn.fetchrow(
            "SELECT id FROM dine_in_sessions WHERE id = $1 AND user_id = $2",
            session_id,
            owner_id,
        )
        if not dinein:
            legacy = await conn.fetchrow(
                "SELECT table_id FROM table_sessions WHERE id = $1 AND user_id = $2",
                session_id,
                owner_id,
            )
            if legacy:
                active = await conn.fetchrow(
                    """
                    SELECT id
                    FROM dine_in_sessions
                    WHERE table_id = $1 AND user_id = $2 AND status = 'active'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    str(legacy["table_id"]),
                    owner_id,
                )
                if active:
                    resolved_session_id = str(active["id"])

    if not resolved_session_id:
        raise NotFoundError("Session", session_id)

    return await _svc.get_session_bill(session_id=resolved_session_id)


@router.post("/sessions/{session_id}/split-bill")
async def split_session_bill(
    session_id: str,
    body: SplitBillIn,
    user: UserContext = Depends(get_current_user),
):
    """Generate split bill allocations (equal/by_item/by_user)."""
    return await _svc.split_bill(
        session_id=session_id,
        split_type=body.split_type,
        parts=body.parts,
        item_splits=body.item_splits,
        user_splits=body.user_splits,
    )


@router.post("/sessions/{session_id}/payments")
async def record_session_payment(
    session_id: str,
    body: SessionPaymentIn,
    user: UserContext = Depends(get_current_user),
):
    """Record a partial/full payment for session settlement."""
    actor = user.owner_id if user.is_branch_user else user.user_id
    return await _svc.record_session_payment(
        session_id=session_id,
        amount=body.amount,
        payment_method=body.payment_method,
        created_by=actor,
        transaction_ref=body.transaction_ref,
        paid_by=body.paid_by,
        notes=body.notes,
    )


@router.post("/sessions/{session_id}/paid-vacate")
async def paid_vacate_session(
    session_id: str,
    user: UserContext = Depends(require_permission("dinein.manage")),
):
    """Close session and free table after full payment validation."""
    actor = user.owner_id if user.is_branch_user else user.user_id
    return await _svc.paid_and_vacate(session_id=session_id, closed_by=actor)
