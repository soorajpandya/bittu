"""Bank Transactions CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete, acc_status_update

router = APIRouter(prefix="/accounting/banktransactions", tags=["Accounting – Bank Transactions"])

TABLE = "acc_bank_transactions"
PK = "transaction_id"
LABEL = "Bank Transaction"


_auth = require_permission("accounting:read")


class BankTransactionCreate(BaseModel):
    account_id: UUID
    date: Optional[str] = None
    amount: float
    transaction_type: str  # deposit / withdrawal
    reference_number: Optional[str] = None
    description: Optional[str] = None
    payee: Optional[str] = None
    currency_code: Optional[str] = None
    exchange_rate: float = 1.0
    from_account_id: Optional[UUID] = None
    to_account_id: Optional[UUID] = None
    customer_id: Optional[UUID] = None
    vendor_id: Optional[UUID] = None
    tax_id: Optional[UUID] = None
    categorized_account_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


class BankTransactionUpdate(BaseModel):
    account_id: Optional[UUID] = None
    date: Optional[str] = None
    amount: Optional[float] = None
    transaction_type: Optional[str] = None
    reference_number: Optional[str] = None
    description: Optional[str] = None
    payee: Optional[str] = None
    currency_code: Optional[str] = None
    exchange_rate: Optional[float] = None
    from_account_id: Optional[UUID] = None
    to_account_id: Optional[UUID] = None
    customer_id: Optional[UUID] = None
    vendor_id: Optional[UUID] = None
    tax_id: Optional[UUID] = None
    categorized_account_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


@router.get("")
async def list_bank_transactions(
    user: UserContext = Depends(_auth),
    account_id: Optional[UUID] = Query(None),
    transaction_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"account_id": account_id, "transaction_type": transaction_type, "status": status}, page=page, per_page=per_page, order_by="date DESC")


@router.post("", status_code=201)
async def create_bank_transaction(body: BankTransactionCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{transaction_id}")
async def get_bank_transaction(transaction_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, transaction_id, user, LABEL)


@router.put("/{transaction_id}")
async def update_bank_transaction(transaction_id: UUID, body: BankTransactionUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, transaction_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{transaction_id}")
async def delete_bank_transaction(transaction_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, transaction_id, user, LABEL)


@router.post("/{transaction_id}/categorize")
async def categorize_transaction(transaction_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, transaction_id, "categorized", user, LABEL)


@router.post("/{transaction_id}/uncategorize")
async def uncategorize_transaction(transaction_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, transaction_id, "uncategorized", user, LABEL)


@router.post("/{transaction_id}/match")
async def match_transaction(transaction_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, transaction_id, "matched", user, LABEL)


# --- Additional endpoints ---


class CategorizeInput(BaseModel):
    reference_id: Optional[UUID] = None
    description: Optional[str] = None


@router.get("/uncategorized/{transaction_id}/match")
async def get_matching_bank_transactions(
    transaction_id: UUID,
    user: UserContext = Depends(_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    source = await acc_get(TABLE, PK, transaction_id, user, LABEL)
    return await acc_list(
        TABLE, user,
        filters={"status": "uncategorized", "amount": source["amount"]},
        page=page, per_page=per_page, order_by="date DESC",
    )


@router.post("/{transaction_id}/unmatch")
async def unmatch_transaction(transaction_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, transaction_id, {"matched_transaction_id": None, "status": "uncategorized"}, user, LABEL)


@router.post("/uncategorized/{transaction_id}/exclude")
async def exclude_transaction(transaction_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, transaction_id, {"status": "excluded"}, user, LABEL)


@router.post("/uncategorized/{transaction_id}/restore")
async def restore_transaction(transaction_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, transaction_id, {"status": "uncategorized"}, user, LABEL)


@router.post("/uncategorized/{transaction_id}/categorize/expenses")
async def categorize_as_expense(transaction_id: UUID, body: CategorizeInput = CategorizeInput(), user: UserContext = Depends(_auth)):
    data = {"category_type": "expense", "status": "categorized", **body.model_dump(exclude_none=True)}
    return await acc_update(TABLE, PK, transaction_id, data, user, LABEL)


@router.post("/uncategorized/{transaction_id}/categorize/vendorpayments")
async def categorize_as_vendor_payment(transaction_id: UUID, body: CategorizeInput = CategorizeInput(), user: UserContext = Depends(_auth)):
    data = {"category_type": "vendor_payment", "status": "categorized", **body.model_dump(exclude_none=True)}
    return await acc_update(TABLE, PK, transaction_id, data, user, LABEL)


@router.post("/uncategorized/{transaction_id}/categorize/customerpayments")
async def categorize_as_customer_payment(transaction_id: UUID, body: CategorizeInput = CategorizeInput(), user: UserContext = Depends(_auth)):
    data = {"category_type": "customer_payment", "status": "categorized", **body.model_dump(exclude_none=True)}
    return await acc_update(TABLE, PK, transaction_id, data, user, LABEL)


@router.post("/uncategorized/{transaction_id}/categorize/creditnoterefunds")
async def categorize_as_credit_note_refund(transaction_id: UUID, body: CategorizeInput = CategorizeInput(), user: UserContext = Depends(_auth)):
    data = {"category_type": "credit_note_refund", "status": "categorized", **body.model_dump(exclude_none=True)}
    return await acc_update(TABLE, PK, transaction_id, data, user, LABEL)


@router.post("/uncategorized/{transaction_id}/categorize/vendorcreditrefunds")
async def categorize_as_vendor_credit_refund(transaction_id: UUID, body: CategorizeInput = CategorizeInput(), user: UserContext = Depends(_auth)):
    data = {"category_type": "vendor_credit_refund", "status": "categorized", **body.model_dump(exclude_none=True)}
    return await acc_update(TABLE, PK, transaction_id, data, user, LABEL)


@router.post("/uncategorized/{transaction_id}/categorize/paymentrefunds")
async def categorize_as_payment_refund(transaction_id: UUID, body: CategorizeInput = CategorizeInput(), user: UserContext = Depends(_auth)):
    data = {"category_type": "payment_refund", "status": "categorized", **body.model_dump(exclude_none=True)}
    return await acc_update(TABLE, PK, transaction_id, data, user, LABEL)


@router.post("/uncategorized/{transaction_id}/categorize/vendorpaymentrefunds")
async def categorize_as_vendor_payment_refund(transaction_id: UUID, body: CategorizeInput = CategorizeInput(), user: UserContext = Depends(_auth)):
    data = {"category_type": "vendor_payment_refund", "status": "categorized", **body.model_dump(exclude_none=True)}
    return await acc_update(TABLE, PK, transaction_id, data, user, LABEL)
