"""Table Session / QR ordering endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.services.table_service import TableSessionService

router = APIRouter(prefix="/tables", tags=["Tables"])
_svc = TableSessionService()


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
