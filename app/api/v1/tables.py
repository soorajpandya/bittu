"""Table Session / QR ordering endpoints."""
import orjson
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator

from app.core.auth import UserContext, require_permission, get_current_user_optional
from app.core.database import get_connection
from app.core.logging import get_logger
from app.core.redis import cache_get, cache_set, cache_delete
from app.services.activity_log_service import log_activity
from app.services.table_service import TableSessionService
from app.services.dinein_session_service import DineInSessionService

_TABLES_LIST_CACHE_TTL = 30  # seconds — table list is invalidated on any table change

router = APIRouter(prefix="/tables", tags=["Tables"])
_svc = TableSessionService()
_dinein_svc = DineInSessionService()
logger = get_logger(__name__)

async def _resolve_active_dinein_session_id(
    *,
    conn,
    session_id: str,
    owner_id: str,
) -> str:
    """
    Compatibility helper:
    - If session_id is already a dine_in_sessions.id → return it
    - Else treat it as legacy table_sessions.id, resolve to table_id, then pick active dine_in_sessions.id
    """
    dinein = await conn.fetchrow(
        "SELECT id FROM dine_in_sessions WHERE id = $1 AND user_id = $2",
        session_id,
        owner_id,
    )
    if dinein:
        return str(dinein["id"])

    legacy = await conn.fetchrow(
        "SELECT table_id FROM table_sessions WHERE id = $1 AND user_id = $2",
        session_id,
        owner_id,
    )
    if not legacy:
        return session_id

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
    return str(active["id"]) if active else session_id


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
    session_token: Optional[str] = None
    table_id: Optional[str] = None
    device_id: str
    device_name: Optional[str] = None

    @model_validator(mode="after")
    def _validate_join_target(self):
        if not self.session_token and not self.table_id:
            raise ValueError("Either session_token or table_id is required")
        return self


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
    user: UserContext = Depends(require_permission("table.read")),
):
    """List all tables for the current user's restaurant."""
    try:
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        cache_key = f"tables_list:{owner_id}"
        try:
            cached = await cache_get(cache_key)
            if cached:
                return orjson.loads(cached)
        except Exception:
            pass

        async with get_connection() as conn:
            rows = await conn.fetch(
                "SELECT id, user_id, restaurant_id, table_number, capacity, status,"
                " is_active, is_occupied, occupied_since, session_token,"
                " current_order_id, created_at, updated_at"
                " FROM restaurant_tables WHERE user_id = $1 ORDER BY table_number ASC",
                owner_id,
            )
        result = [dict(r) for r in rows]
        try:
            await cache_set(cache_key, orjson.dumps(result, default=str).decode(), ttl=_TABLES_LIST_CACHE_TTL)
        except Exception:
            pass
        return result
    except Exception as e:
        logger.warning("list_tables_failed", error=str(e), user_id=user.user_id)
        return []


@router.post("", status_code=201)
async def create_table(
    body: CreateTableIn,
    user: UserContext = Depends(require_permission("table.manage")),
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
            result = dict(row)
            await log_activity(
                user_id=user.user_id,
                branch_id=user.branch_id,
                action="table.created",
                entity_type="table",
                entity_id=str(result.get("id")) if result.get("id") else None,
                metadata={"table_number": body.table_number},
            )
            try:
                await cache_delete(f"tables_list:{owner_id}")
            except Exception:
                pass
            return result
    except Exception as e:
        logger.warning("create_table_failed", error=str(e), user_id=user.user_id)
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=500, content={"detail": "Failed to create table"})


class UpdateTableIn(BaseModel):
    table_number: Optional[str] = None
    capacity: Optional[int] = None
    status: Optional[str] = None
    is_active: Optional[bool] = None


@router.patch("/{table_id}")
async def update_table(
    table_id: str,
    body: UpdateTableIn,
    user: UserContext = Depends(require_permission("table.manage")),
):
    """Update a restaurant table."""
    try:
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        if not updates:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=400, content={"detail": "No fields to update"})

        set_clauses = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(updates))
        values = list(updates.values())

        async with get_connection() as conn:
            row = await conn.fetchrow(
                f"UPDATE restaurant_tables SET {set_clauses} WHERE id = $1 AND user_id = ${len(values)+2} RETURNING *",
                table_id, *values, owner_id,
            )
            if not row:
                from fastapi.responses import JSONResponse
                return JSONResponse(status_code=404, content={"detail": "Table not found"})
            result = dict(row)
            await log_activity(
                user_id=user.user_id,
                branch_id=user.branch_id,
                action="table.updated",
                entity_type="table",
                entity_id=table_id,
                metadata={"updated_fields": list(updates.keys())},
            )
            try:
                await cache_delete(f"tables_list:{owner_id}")
            except Exception:
                pass
            return result
    except Exception as e:
        logger.warning("update_table_failed", error=str(e), user_id=user.user_id)
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=500, content={"detail": "Failed to update table"})


@router.delete("/{table_id}")
async def delete_table(
    table_id: str,
    user: UserContext = Depends(require_permission("table.manage")),
):
    """Delete a restaurant table."""
    try:
        owner_id = user.owner_id if user.is_branch_user else user.user_id
        async with get_connection() as conn:
            result = await conn.execute(
                "DELETE FROM restaurant_tables WHERE id = $1 AND user_id = $2",
                table_id, owner_id,
            )
            if "DELETE 0" in result:
                from fastapi.responses import JSONResponse
                return JSONResponse(status_code=404, content={"detail": "Table not found"})
            await log_activity(
                user_id=user.user_id,
                branch_id=user.branch_id,
                action="table.deleted",
                entity_type="table",
                entity_id=table_id,
                metadata={},
            )
            try:
                await cache_delete(f"tables_list:{owner_id}")
            except Exception:
                pass
            return {"status": "deleted"}
    except Exception as e:
        logger.warning("delete_table_failed", error=str(e), user_id=user.user_id)
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=500, content={"detail": "Failed to delete table"})


@router.post("/sessions")
async def start_session(
    body: StartSessionIn,
    user: UserContext = Depends(require_permission("table.start")),
):
    result = await _svc.start_session(
        user=user,
        table_id=body.table_id,
        branch_id=body.branch_id,
        customer_name=body.customer_name,
    )
    await log_activity(
        user_id=user.user_id,
        branch_id=user.branch_id,
        action="table.session_started",
        entity_type="table_session",
        entity_id=str(result.get("session_id")) if isinstance(result, dict) else None,
        metadata={"table_id": body.table_id},
    )
    return result


@router.post("/sessions/join")
async def join_session(
    body: JoinSessionIn,
    user: Optional[UserContext] = Depends(get_current_user_optional),
):
    # Public/QR join: token is enough (no JWT required)
    if body.session_token:
        return await _svc.join_session(
            session_token=body.session_token,
            device_id=body.device_id,
            device_name=body.device_name,
        )

    # Admin/POS join-by-table: requires JWT so we can safely create a session
    if not user:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=401, content={"detail": "Authorization required for table_id join"})

    return await _svc.join_or_create_session_by_table(
        user=user,
        table_id=body.table_id,  # type: ignore[arg-type]
        device_id=body.device_id,
        device_name=body.device_name,
    )


@router.post("/cart/add")
async def add_to_cart(
    body: AddToCartIn,
    user: UserContext = Depends(require_permission("table.start")),
):
    return await _svc.add_to_cart_admin(
        user=user,
        session_id=body.session_id,
        items=[i.model_dump() for i in body.items],
    )


class AdminPlaceOrderIn(BaseModel):
    notes: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    payment_method: Optional[str] = "cash"


@router.post("/sessions/{session_id}/place-order")
async def admin_place_order(
    session_id: str,
    body: AdminPlaceOrderIn = AdminPlaceOrderIn(),
    user: UserContext = Depends(require_permission("table.start")),
):
    """Place order from cart using session_id (admin/POS flow).

    Use this instead of /qr/place-order when the session was created via the
    admin app (/tables/cart/add flow).  Links the order to session_orders so
    subsequent payment calls compute grand_total correctly.
    """
    return await _svc.place_order_admin(
        user=user,
        session_id=session_id,
        notes=body.notes,
        customer_name=body.customer_name,
        customer_phone=body.customer_phone,
        payment_method=body.payment_method or "cash",
    )


@router.get("/cart/{session_id}")
async def get_cart(
    session_id: str,
    user: UserContext = Depends(require_permission("table.read")),
):
    owner_id = user.owner_id if user.is_branch_user else user.user_id
    async with get_connection() as conn:
        resolved_session_id = await _resolve_active_dinein_session_id(
            conn=conn,
            session_id=session_id,
            owner_id=owner_id,
        )
    return await _svc.get_cart(session_id=resolved_session_id)


@router.delete("/cart/remove")
async def remove_from_cart(
    body: RemoveCartItemIn,
    user: UserContext = Depends(require_permission("table.start")),
):
    return await _svc.remove_from_cart(
        user=user,
        session_id=body.session_id,
        item_id=body.item_id,
    )


@router.post("/sessions/{session_id}/end")
async def end_session(
    session_id: str,
    user: UserContext = Depends(require_permission("table.close")),
):
    result = await _svc.end_session(user=user, session_id=session_id)
    await log_activity(
        user_id=user.user_id,
        branch_id=user.branch_id,
        action="table.session_closed",
        entity_type="table_session",
        entity_id=session_id,
        metadata={},
    )
    return result


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


# ── QR Call Waiter (public) ──

class QRCallWaiterIn(BaseModel):
    session_token: str
    request_type: Optional[str] = "assistance"  # assistance | bill | water


@router.post("/qr/call-waiter")
async def qr_call_waiter(body: QRCallWaiterIn):
    """Customer requests waiter assistance from QR interface."""
    return await _svc.call_waiter(body.model_dump())


# ── Admin: Mark Paid & Vacate ──

class MarkPaidVacateIn(BaseModel):
    order_id: Optional[str] = None


class SessionSplitBillIn(BaseModel):
    split_type: str = "equal"
    parts: int = 1
    item_splits: Optional[list[dict]] = None
    user_splits: Optional[list[dict]] = None


class SessionPaymentIn(BaseModel):
    amount: float = Field(gt=0)
    payment_method: str
    transaction_ref: Optional[str] = None
    paid_by: Optional[str] = None
    notes: Optional[str] = None


@router.get("/sessions/{session_id}/bill")
async def get_session_bill(
    session_id: str,
    user: UserContext = Depends(require_permission("billing.generate")),
):
    """POS alias: get full bill for a table session."""
    owner_id = user.owner_id if user.is_branch_user else user.user_id
    async with get_connection() as conn:
        resolved_session_id = await _resolve_active_dinein_session_id(
            conn=conn,
            session_id=session_id,
            owner_id=owner_id,
        )
    return await _dinein_svc.get_session_bill(resolved_session_id)


@router.post("/sessions/{session_id}/split-bill")
async def split_bill(
    session_id: str,
    body: SessionSplitBillIn,
    user: UserContext = Depends(require_permission("billing.generate")),
):
    """POS alias: split session bill equally/by-item/by-user."""
    owner_id = user.owner_id if user.is_branch_user else user.user_id
    async with get_connection() as conn:
        resolved_session_id = await _resolve_active_dinein_session_id(
            conn=conn,
            session_id=session_id,
            owner_id=owner_id,
        )
    return await _dinein_svc.split_bill(
        session_id=resolved_session_id,
        split_type=body.split_type,
        parts=body.parts,
        item_splits=body.item_splits,
        user_splits=body.user_splits,
    )


@router.post("/sessions/{session_id}/payments")
async def add_session_payment(
    session_id: str,
    body: SessionPaymentIn,
    user: UserContext = Depends(require_permission("payment.create")),
):
    """POS alias: record partial/full payment against session."""
    actor = user.owner_id if user.is_branch_user else user.user_id
    async with get_connection() as conn:
        resolved_session_id = await _resolve_active_dinein_session_id(
            conn=conn,
            session_id=session_id,
            owner_id=actor,
        )
    result = await _dinein_svc.record_session_payment(
        session_id=resolved_session_id,
        amount=body.amount,
        payment_method=body.payment_method,
        created_by=actor,
        transaction_ref=body.transaction_ref,
        paid_by=body.paid_by,
        notes=body.notes,
    )
    await log_activity(
        user_id=user.user_id,
        branch_id=user.branch_id,
        action="payment.session_payment",
        entity_type="table_session",
        entity_id=resolved_session_id,
        metadata={"amount": body.amount, "payment_method": body.payment_method},
    )
    return result


@router.post("/sessions/{session_id}/paid-vacate")
async def mark_paid_and_vacate(
    session_id: str,
    body: MarkPaidVacateIn = MarkPaidVacateIn(),
    user: UserContext = Depends(require_permission("table.close")),
):
    """Mark order as paid and end the table session."""
    actor = user.owner_id if user.is_branch_user else user.user_id
    async with get_connection() as conn:
        resolved_session_id = await _resolve_active_dinein_session_id(
            conn=conn,
            session_id=session_id,
            owner_id=actor,
        )
    result = await _dinein_svc.paid_and_vacate(session_id=resolved_session_id, closed_by=actor)
    await log_activity(
        user_id=user.user_id,
        branch_id=user.branch_id,
        action="table.paid_and_vacated",
        entity_type="table_session",
        entity_id=resolved_session_id,
        metadata={},
    )
    return result
