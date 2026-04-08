"""Customer Payments CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete, acc_sub_list, acc_sub_create, acc_sub_get, acc_sub_update, acc_sub_delete, acc_bulk_delete

router = APIRouter(prefix="/accounting/customerpayments", tags=["Accounting – Customer Payments"])

TABLE = "acc_customer_payments"
PK = "customer_payment_id"
LABEL = "Customer Payment"


_auth = require_permission("accounting:read")


class InvoicePayment(BaseModel):
    invoice_id: UUID
    amount_applied: float


class CustomerPaymentCreate(BaseModel):
    customer_id: UUID
    payment_mode: str
    amount: float
    date: Optional[str] = None
    invoices: Optional[list[InvoicePayment]] = None
    payment_number: Optional[str] = None
    reference_number: Optional[str] = None
    description: Optional[str] = None
    exchange_rate: float = 1.0
    account_id: Optional[UUID] = None
    bank_charges: float = 0
    tax_amount_withheld: float = 0
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


class RefundCreate(BaseModel):
    date: Optional[str] = None
    refund_mode: Optional[str] = None
    reference_number: Optional[str] = None
    amount: Optional[float] = None
    account_id: Optional[UUID] = None
    description: Optional[str] = None


class RefundUpdate(BaseModel):
    date: Optional[str] = None
    refund_mode: Optional[str] = None
    reference_number: Optional[str] = None
    amount: Optional[float] = None
    account_id: Optional[UUID] = None
    description: Optional[str] = None


class BulkDeleteInput(BaseModel):
    customer_payment_ids: list[UUID]


class CustomFieldUpdate(BaseModel):
    custom_fields: list


class CustomerPaymentUpdate(BaseModel):
    customer_id: Optional[UUID] = None
    payment_mode: Optional[str] = None
    amount: Optional[float] = None
    date: Optional[str] = None
    invoices: Optional[list[InvoicePayment]] = None
    payment_number: Optional[str] = None
    reference_number: Optional[str] = None
    description: Optional[str] = None
    exchange_rate: Optional[float] = None
    account_id: Optional[UUID] = None
    bank_charges: Optional[float] = None
    tax_amount_withheld: Optional[float] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


@router.get("")
async def list_customer_payments(
    user: UserContext = Depends(_auth),
    customer_id: Optional[UUID] = Query(None),
    payment_mode: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"customer_id": customer_id, "payment_mode": payment_mode}, page=page, per_page=per_page, order_by="date DESC")


@router.post("", status_code=201)
async def create_customer_payment(body: CustomerPaymentCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    if data.get("invoices"):
        data["invoices"] = [i.model_dump() if hasattr(i, "model_dump") else i for i in data["invoices"]]
    return await acc_create(TABLE, data, user)


@router.get("/{customer_payment_id}")
async def get_customer_payment(customer_payment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, customer_payment_id, user, LABEL)


@router.put("/{customer_payment_id}")
async def update_customer_payment(customer_payment_id: UUID, body: CustomerPaymentUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    if data.get("invoices"):
        data["invoices"] = [i.model_dump() if hasattr(i, "model_dump") else i for i in data["invoices"]]
    return await acc_update(TABLE, PK, customer_payment_id, data, user, LABEL)


@router.delete("/{customer_payment_id}")
async def delete_customer_payment(customer_payment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, customer_payment_id, user, LABEL)


@router.put("/customerpayments")
async def update_customer_payment_custom(body: CustomerPaymentUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    if data.get("invoices"):
        data["invoices"] = [i.model_dump() if hasattr(i, "model_dump") else i for i in data["invoices"]]
    return await acc_update(TABLE, PK, None, data, user, LABEL)


@router.post("/bulk-delete")
async def bulk_delete_customer_payments(body: BulkDeleteInput, user: UserContext = Depends(_auth)):
    return await acc_bulk_delete(TABLE, PK, body.customer_payment_ids, user, LABEL)


@router.put("/customerpayment/{customer_payment_id}/customfields")
async def update_customer_payment_custom_fields(customer_payment_id: UUID, body: CustomFieldUpdate, user: UserContext = Depends(_auth)):
    data = {"custom_fields": body.custom_fields}
    return await acc_update(TABLE, PK, customer_payment_id, data, user, LABEL)


# --- Refund sub-resource ---
SUB_TABLE = "acc_customer_payment_refunds"
SUB_PK = "refund_id"
PARENT_COL = "customer_payment_id"


@router.get("/{customer_payment_id}/refunds")
async def list_customer_payment_refunds(customer_payment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_list(SUB_TABLE, PARENT_COL, customer_payment_id, user)


@router.post("/{customer_payment_id}/refunds", status_code=201)
async def create_customer_payment_refund(customer_payment_id: UUID, body: RefundCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    return await acc_sub_create(SUB_TABLE, PARENT_COL, customer_payment_id, data, user)


@router.get("/{customer_payment_id}/refunds/{refund_id}")
async def get_customer_payment_refund(customer_payment_id: UUID, refund_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_get(SUB_TABLE, SUB_PK, refund_id, PARENT_COL, customer_payment_id, user)


@router.put("/{customer_payment_id}/refunds/{refund_id}")
async def update_customer_payment_refund(customer_payment_id: UUID, refund_id: UUID, body: RefundUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    return await acc_sub_update(SUB_TABLE, SUB_PK, refund_id, PARENT_COL, customer_payment_id, data, user)


@router.delete("/{customer_payment_id}/refunds/{refund_id}")
async def delete_customer_payment_refund(customer_payment_id: UUID, refund_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_delete(SUB_TABLE, SUB_PK, refund_id, PARENT_COL, customer_payment_id, user)
