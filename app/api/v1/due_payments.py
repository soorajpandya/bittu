"""Due Payment endpoints."""
from typing import Optional
from datetime import date
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, require_role
from app.services.due_payment_service import DuePaymentService

router = APIRouter(prefix="/due-payments", tags=["Due Payments"])
_svc = DuePaymentService()


class DuePaymentCreate(BaseModel):
    customer_id: Optional[int] = None
    order_id: Optional[str] = None
    total_amount: float
    paid_amount: Optional[float] = 0
    due_amount: Optional[float] = None
    status: Optional[str] = "pending"
    due_date: Optional[date] = None
    notes: Optional[str] = None


class RecordPayment(BaseModel):
    amount: float


class UpdateStatus(BaseModel):
    status: str


@router.get("")
async def list_due_payments(
    status: Optional[str] = None,
    customer_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_role("owner", "manager", "cashier")),
):
    return await _svc.list_due_payments(user, status=status, customer_id=customer_id, limit=limit, offset=offset)


@router.get("/{dp_id}")
async def get_due_payment(
    dp_id: int,
    user: UserContext = Depends(require_role("owner", "manager", "cashier")),
):
    return await _svc.get_due_payment(user, dp_id)


@router.post("", status_code=201)
async def create_due_payment(
    body: DuePaymentCreate,
    user: UserContext = Depends(require_role("owner", "manager", "cashier")),
):
    return await _svc.create_due_payment(user, body.model_dump())


@router.post("/{dp_id}/pay")
async def record_payment(
    dp_id: int,
    body: RecordPayment,
    user: UserContext = Depends(require_role("owner", "manager", "cashier")),
):
    return await _svc.record_payment(user, dp_id, body.amount)


@router.patch("/{dp_id}/status")
async def update_status(
    dp_id: int,
    body: UpdateStatus,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.update_status(user, dp_id, body.status)
