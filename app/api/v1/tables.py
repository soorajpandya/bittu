"""Table Session / QR ordering endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission, require_role
from app.core.database import get_connection
from app.core.logging import get_logger
from app.services.table_service import TableSessionService

router = APIRouter(prefix="/tables", tags=["Tables"])
_svc = TableSessionService()
logger = get_logger(__name__)


class CreateTableIn(BaseModel):
    table_number: str
    capacity: Optional[int] = 4
    status: Optional[str] = "available"
    is_active: Optional[bool] = True


class StartSessionIn(BaseModel):
    branch_id: str
    table_id: str
    customer_name: Optional[str] = None


class JoinSessionIn(BaseModel):
    session_token: str
    device_id: str


class CartItemIn(BaseModel):
    item_id: str
    variant_id: Optional[str] = None
    quantity: int = Field(ge=1)
    notes: Optional[str] = None


class AddToCartIn(BaseModel):
    session_id: str
    items: list[CartItemIn]


class RemoveCartItemIn(BaseModel):
    session_id: str
    item_id: str


@router.get("")
async def list_tables(
    user: UserContext = Depends(require_role("owner", "manager", "cashier", "chef", "waiter", "staff")),
):
    """List all tables for the current user's restaurant."""
    try:
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM restaurant_tables WHERE user_id = $1 ORDER BY table_number ASC",
                owner_id,
            )
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("list_tables_failed", error=str(e), user_id=user.user_id)
        return []


@router.post("", status_code=201)
async def create_table(
    body: CreateTableIn,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    """Create a new restaurant table."""
    try:
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO restaurant_tables (user_id, restaurant_id, table_number, capacity, status, is_active)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING *
                """,
                owner_id,
                user.restaurant_id,
                body.table_number,
                body.capacity,
                body.status,
                body.is_active,
            )
            return dict(row)
    except Exception as e:
        logger.warning("create_table_failed", error=str(e), user_id=user.user_id)
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=500, content={"detail": "Failed to create table"})


@router.post("/sessions")
async def start_session(
    body: StartSessionIn,
    user: UserContext = Depends(require_permission("tables.manage")),
):
    return await _svc.start_session(
        user=user,
        branch_id=body.branch_id,
        table_id=body.table_id,
        customer_name=body.customer_name,
    )


@router.post("/sessions/join")
async def join_session(body: JoinSessionIn):
    return await _svc.join_session(
        session_token=body.session_token,
        device_id=body.device_id,
    )


@router.post("/cart/add")
async def add_to_cart(
    body: AddToCartIn,
    user: UserContext = Depends(require_permission("tables.manage")),
):
    return await _svc.add_to_cart(
        user=user,
        session_id=body.session_id,
        items=[i.model_dump() for i in body.items],
    )


@router.get("/cart/{session_id}")
async def get_cart(
    session_id: str,
    user: UserContext = Depends(require_permission("tables.manage")),
):
    return await _svc.get_cart(user=user, session_id=session_id)


@router.delete("/cart/remove")
async def remove_from_cart(
    body: RemoveCartItemIn,
    user: UserContext = Depends(require_permission("tables.manage")),
):
    return await _svc.remove_from_cart(
        user=user,
        session_id=body.session_id,
        item_id=body.item_id,
    )


@router.post("/sessions/{session_id}/end")
async def end_session(
    session_id: str,
    user: UserContext = Depends(require_permission("tables.manage")),
):
    return await _svc.end_session(user=user, session_id=session_id)


# ── QR Ordering Endpoints (public — customer-facing, no JWT) ──


class QRScanIn(BaseModel):
    restaurant_id: str
    table_id: str
    device_id: str


class QRCartActionIn(BaseModel):
    session_token: str
    action: Optional[str] = "add"  # add | update | remove | clear
    item_id: Optional[int] = None
    variant_id: Optional[str] = None
    quantity: Optional[int] = 1
    addons: Optional[list] = []
    extras: Optional[list] = []
    notes: Optional[str] = None
    device_id: Optional[str] = None
    cart_item_id: Optional[str] = None
    item_name: Optional[str] = None
    unit_price: Optional[float] = None


class QRPlaceOrderIn(BaseModel):
    session_token: str
    device_id: Optional[str] = None
    notes: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    payment_method: Optional[str] = "cash"


@router.post("/qr/scan")
async def qr_scan(body: QRScanIn):
    """Customer scans QR code — creates or resumes a table session."""
    return await _svc.qr_scan(
        restaurant_id=body.restaurant_id,
        table_id=body.table_id,
        device_id=body.device_id,
    )


@router.get("/qr/menu")
async def qr_menu(
    restaurant_id: str = Query(...),
):
    """Return full menu for QR ordering."""
    return await _svc.qr_menu(restaurant_id=restaurant_id)


@router.get("/qr/cart")
async def qr_get_cart(session_token: str = Query(...)):
    """Get cart contents for a QR session."""
    return await _svc.qr_get_cart(session_token=session_token)


@router.post("/qr/cart")
async def qr_cart_action(body: QRCartActionIn):
    """Add / update / remove / clear items in QR cart."""
    return await _svc.qr_cart_action(body.model_dump())


@router.post("/qr/place-order")
async def qr_place_order(body: QRPlaceOrderIn):
    """Place a dine-in order from QR cart."""
    return await _svc.qr_place_order(body.model_dump())


@router.get("/qr/order-status")
async def qr_order_status(
    session_token: str = Query(...),
):
    """Get all orders for this session with kitchen status."""
    return await _svc.qr_order_status(session_token=session_token)
