"""Vendor Payments CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import acc_list, acc_get, acc_create, acc_update, acc_delete, acc_sub_list, acc_sub_create, acc_sub_get, acc_sub_update, acc_sub_delete, acc_email_get, acc_email_send, acc_bulk_delete

router = APIRouter(prefix="/accounting/vendorpayments", tags=["Accounting – Vendor Payments"])

TABLE = "acc_vendor_payments"
PK = "payment_id"
LABEL = "Vendor Payment"


_auth = require_permission("accounting:read")


class BillPayment(BaseModel):
    bill_id: UUID
    amount_applied: float


class RefundCreate(BaseModel):
    date: str
    refund_mode: str
    reference_number: Optional[str] = None
    amount: float
    account_id: UUID
    description: Optional[str] = None


class RefundUpdate(BaseModel):
    date: Optional[str] = None
    refund_mode: Optional[str] = None
    reference_number: Optional[str] = None
    amount: Optional[float] = None
    account_id: Optional[UUID] = None
    description: Optional[str] = None


class EmailInput(BaseModel):
    to_mail_ids: list[str]
    cc_mail_ids: Optional[list[str]] = None
    subject: str
    body: str


class BulkDeleteInput(BaseModel):
    vendor_payment_ids: list[UUID]


class VendorPaymentCreate(BaseModel):
    vendor_id: UUID
    payment_mode: str
    amount: float
    date: Optional[str] = None
    bills: Optional[list[BillPayment]] = None
    payment_number: Optional[str] = None
    reference_number: Optional[str] = None
    description: Optional[str] = None
    exchange_rate: float = 1.0
    account_id: Optional[UUID] = None
    paid_through_account_id: Optional[UUID] = None
    bank_charges: float = 0
    tax_amount_withheld: float = 0
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


class VendorPaymentUpdate(BaseModel):
    vendor_id: Optional[UUID] = None
    payment_mode: Optional[str] = None
    amount: Optional[float] = None
    date: Optional[str] = None
    bills: Optional[list[BillPayment]] = None
    payment_number: Optional[str] = None
    reference_number: Optional[str] = None
    description: Optional[str] = None
    exchange_rate: Optional[float] = None
    account_id: Optional[UUID] = None
    paid_through_account_id: Optional[UUID] = None
    bank_charges: Optional[float] = None
    tax_amount_withheld: Optional[float] = None
    location_id: Optional[UUID] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None


@router.get("")
async def list_vendor_payments(
    user: UserContext = Depends(_auth),
    vendor_id: Optional[UUID] = Query(None),
    payment_mode: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(TABLE, user, filters={"vendor_id": vendor_id, "payment_mode": payment_mode}, page=page, per_page=per_page, order_by="date DESC")


@router.post("", status_code=201)
async def create_vendor_payment(body: VendorPaymentCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    if data.get("bills"):
        data["bills"] = [b.model_dump() if hasattr(b, "model_dump") else b for b in data["bills"]]
    return await acc_create(TABLE, data, user)


@router.get("/{payment_id}")
async def get_vendor_payment(payment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, payment_id, user, LABEL)


@router.put("/{payment_id}")
async def update_vendor_payment(payment_id: UUID, body: VendorPaymentUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    if data.get("bills"):
        data["bills"] = [b.model_dump() if hasattr(b, "model_dump") else b for b in data["bills"]]
    return await acc_update(TABLE, PK, payment_id, data, user, LABEL)


@router.delete("/{payment_id}")
async def delete_vendor_payment(payment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, payment_id, user, LABEL)


@router.put("/vendorpayments")
async def update_vendor_payment_custom(body: VendorPaymentUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    if data.get("bills"):
        data["bills"] = [b.model_dump() if hasattr(b, "model_dump") else b for b in data["bills"]]
    return await acc_update(TABLE, PK, None, data, user, LABEL)


@router.post("/bulk-delete")
async def bulk_delete_vendor_payments(body: BulkDeleteInput, user: UserContext = Depends(_auth)):
    return await acc_bulk_delete(TABLE, PK, body.vendor_payment_ids, user, LABEL)


# --- Refund sub-resource ---
SUB_TABLE = "acc_vendor_payment_refunds"
SUB_PK = "vendorpayment_refund_id"
PARENT_COL = "vendor_payment_id"


@router.get("/{payment_id}/refunds")
async def list_vendor_payment_refunds(payment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_list(SUB_TABLE, PARENT_COL, payment_id, user)


@router.post("/{payment_id}/refunds", status_code=201)
async def create_vendor_payment_refund(payment_id: UUID, body: RefundCreate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    return await acc_sub_create(SUB_TABLE, PARENT_COL, payment_id, data, user)


@router.get("/{payment_id}/refunds/{vendorpayment_refund_id}")
async def get_vendor_payment_refund(payment_id: UUID, vendorpayment_refund_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_get(SUB_TABLE, SUB_PK, vendorpayment_refund_id, PARENT_COL, payment_id, user)


@router.put("/{payment_id}/refunds/{vendorpayment_refund_id}")
async def update_vendor_payment_refund(payment_id: UUID, vendorpayment_refund_id: UUID, body: RefundUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    return await acc_sub_update(SUB_TABLE, SUB_PK, vendorpayment_refund_id, PARENT_COL, payment_id, data, user)


@router.delete("/{payment_id}/refunds/{vendorpayment_refund_id}")
async def delete_vendor_payment_refund(payment_id: UUID, vendorpayment_refund_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_delete(SUB_TABLE, SUB_PK, vendorpayment_refund_id, PARENT_COL, payment_id, user)


# --- Email ---


@router.get("/{payment_id}/email")
async def get_vendor_payment_email(payment_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_email_get(TABLE, PK, payment_id, user, LABEL)


@router.post("/{payment_id}/email")
async def email_vendor_payment(payment_id: UUID, body: EmailInput, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_none=True)
    return await acc_email_send(TABLE, PK, payment_id, data, user, LABEL)
