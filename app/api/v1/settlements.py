"""Settlement and Accounting Rules API endpoints."""
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.services.settlement_service import settlement_service
from app.services.accounting_rules_engine import rules_engine

router = APIRouter(prefix="/settlements", tags=["Settlements"])
rules_router = APIRouter(prefix="/accounting/rules", tags=["Accounting Rules"])
logger = get_logger(__name__)


# ── Settlement Models ──────────────────────────────────────────────────────────

class RecordSettlementRequest(BaseModel):
    gateway: str  # razorpay, cashfree, phonepe
    settlement_id: Optional[str] = None
    settlement_date: Optional[date] = None
    gross_amount: float
    gateway_fee: float = 0
    tax_on_fee: float = 0
    net_amount: Optional[float] = None
    payment_ids: Optional[list[str]] = None
    notes: str = ""


class ReconcileRequest(BaseModel):
    notes: str = ""


# ── Settlement Endpoints ───────────────────────────────────────────────────────

@router.get("")
async def list_settlements(
    status: Optional[str] = Query(None, description="pending|received|reconciled|disputed"),
    gateway: Optional[str] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("settlement.read")),
):
    """List payment gateway settlements with filters."""
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    return await settlement_service.list_settlements(
        restaurant_id=restaurant_id,
        status=status,
        gateway=gateway,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        offset=offset,
    )


@router.post("", status_code=201)
async def record_settlement(
    body: RecordSettlementRequest,
    user: UserContext = Depends(require_permission("settlement.write")),
):
    """
    Record a payment gateway settlement.

    Creates journal entries:
      1. DR Bank, CR PG Clearing (net amount deposited)
      2. DR Gateway Charges + Tax, CR PG Clearing (fees deducted)
    """
    uid = user.owner_id if user.is_branch_user else user.user_id
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")

    try:
        return await settlement_service.record_settlement(
            restaurant_id=restaurant_id,
            branch_id=user.branch_id,
            gateway=body.gateway,
            settlement_id=body.settlement_id,
            settlement_date=body.settlement_date,
            gross_amount=body.gross_amount,
            gateway_fee=body.gateway_fee,
            tax_on_fee=body.tax_on_fee,
            net_amount=body.net_amount,
            payment_ids=body.payment_ids,
            notes=body.notes,
            created_by=uid,
        )
    except (ValidationError, Exception) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{settlement_id}/reconcile")
async def reconcile_settlement(
    settlement_id: str,
    body: ReconcileRequest,
    user: UserContext = Depends(require_permission("settlement.reconcile")),
):
    """Mark a settlement as reconciled (verified against bank statement)."""
    uid = user.owner_id if user.is_branch_user else user.user_id
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")

    try:
        return await settlement_service.reconcile_settlement(
            settlement_db_id=settlement_id,
            restaurant_id=restaurant_id,
            reconciled_by=uid,
            notes=body.notes,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/clearing-balance")
async def clearing_balance(
    user: UserContext = Depends(require_permission("settlement.read")),
):
    """
    Get current PG Clearing account balance.
    Positive = money captured by gateways but not yet settled to bank.
    """
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    return await settlement_service.get_clearing_balance(restaurant_id)


@router.get("/unsettled-payments")
async def unsettled_payments(
    gateway: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    user: UserContext = Depends(require_permission("settlement.read")),
):
    """Find online payments not yet included in any settlement batch."""
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    return await settlement_service.get_unsettled_payments(
        restaurant_id=restaurant_id, gateway=gateway, limit=limit,
    )


# ── Accounting Rules Models ────────────────────────────────────────────────────

class CreateRuleRequest(BaseModel):
    event_type: str
    rule_name: str
    debit_account_code: str
    credit_account_code: str
    amount_field: str = "amount"
    amount_multiplier: float = 1.0
    conditions: Optional[dict] = None
    priority: int = 100
    description: str = ""
    reference_type_override: Optional[str] = None
    description_template: Optional[str] = None


class UpdateRuleRequest(BaseModel):
    rule_name: Optional[str] = None
    description: Optional[str] = None
    debit_account_code: Optional[str] = None
    credit_account_code: Optional[str] = None
    amount_field: Optional[str] = None
    amount_multiplier: Optional[float] = None
    conditions: Optional[dict] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None
    reference_type_override: Optional[str] = None
    description_template: Optional[str] = None


# ── Accounting Rules Endpoints ─────────────────────────────────────────────────

@rules_router.get("")
async def list_rules(
    event_type: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("accounting.rules.read")),
):
    """List accounting rules, optionally filtered by event type."""
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    return await rules_engine.list_rules(restaurant_id, event_type=event_type)


@rules_router.post("", status_code=201)
async def create_rule(
    body: CreateRuleRequest,
    user: UserContext = Depends(require_permission("accounting.rules.write")),
):
    """
    Create a custom accounting rule.

    Rules override the default hardcoded journal entry patterns.
    When an event fires, the engine checks custom rules first (by priority),
    and falls back to defaults if no rule matches.
    """
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")

    try:
        return await rules_engine.create_rule(
            restaurant_id=restaurant_id,
            event_type=body.event_type,
            rule_name=body.rule_name,
            debit_account_code=body.debit_account_code,
            credit_account_code=body.credit_account_code,
            amount_field=body.amount_field,
            amount_multiplier=body.amount_multiplier,
            conditions=body.conditions,
            priority=body.priority,
            description=body.description,
            reference_type_override=body.reference_type_override,
            description_template=body.description_template,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@rules_router.put("/{rule_id}")
async def update_rule(
    rule_id: str,
    body: UpdateRuleRequest,
    user: UserContext = Depends(require_permission("accounting.rules.write")),
):
    """Update an existing accounting rule."""
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")

    try:
        return await rules_engine.update_rule(
            rule_id=rule_id,
            restaurant_id=restaurant_id,
            **body.model_dump(exclude_none=True),
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@rules_router.delete("/{rule_id}")
async def delete_rule(
    rule_id: str,
    user: UserContext = Depends(require_permission("accounting.rules.write")),
):
    """Soft-delete an accounting rule (deactivates it)."""
    restaurant_id = user.restaurant_id
    if not restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    return await rules_engine.delete_rule(rule_id=rule_id, restaurant_id=restaurant_id)
