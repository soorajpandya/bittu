"""Cash Transaction endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, require_role
from app.services.cash_transaction_service import CashTransactionService

router = APIRouter(prefix="/cash-transactions", tags=["Cash Transactions"])
_svc = CashTransactionService()


class CashTxCreate(BaseModel):
    type: str
    amount: float
    description: Optional[str] = None
    category: Optional[str] = None
    payment_method: Optional[str] = "cash"


@router.get("")
async def list_transactions(
    type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_role("owner", "manager", "cashier")),
):
    return await _svc.list_transactions(user, tx_type=type, limit=limit, offset=offset)


@router.get("/{tx_id}")
async def get_transaction(
    tx_id: int,
    user: UserContext = Depends(require_role("owner", "manager", "cashier")),
):
    return await _svc.get_transaction(user, tx_id)


@router.post("", status_code=201)
async def create_transaction(
    body: CashTxCreate,
    user: UserContext = Depends(require_role("owner", "manager", "cashier")),
):
    return await _svc.create_transaction(user, body.model_dump())


@router.delete("/{tx_id}")
async def delete_transaction(
    tx_id: int,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.delete_transaction(user, tx_id)
