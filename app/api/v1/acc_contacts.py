"""Accounting Contacts (Vendors/Customers) CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.services.accounting_crud import (
    acc_list, acc_get, acc_create, acc_update, acc_delete, acc_status_update,
    acc_comments_list, acc_email_get, acc_email_send,
    acc_sub_list, acc_sub_create, acc_sub_get, acc_sub_update, acc_sub_delete,
)

router = APIRouter(prefix="/accounting/contacts", tags=["Accounting – Contacts"])

TABLE = "acc_contacts"
PK = "contact_id"
LABEL = "Contact"


_auth = require_permission("accounting:read")


SUB_TABLE = "acc_contact_addresses"
SUB_PK = "address_id"
PARENT_COL = "contact_id"


class ContactCreate(BaseModel):
    contact_name: str
    company_name: Optional[str] = None
    contact_type: str = "customer"
    customer_sub_type: Optional[str] = None
    website: Optional[str] = None
    language_code: str = "en"
    credit_limit: Optional[float] = None
    contact_number: Optional[str] = None
    currency_id: Optional[UUID] = None
    payment_terms: Optional[int] = None
    payment_terms_label: Optional[str] = None
    notes: Optional[str] = None
    billing_address: Optional[dict] = None
    shipping_address: Optional[dict] = None
    default_templates: Optional[dict] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    is_portal_enabled: bool = False
    vat_reg_no: Optional[str] = None
    tax_reg_no: Optional[str] = None
    country_code: Optional[str] = None
    vat_treatment: Optional[str] = None
    tax_treatment: Optional[str] = None
    tax_regime: Optional[str] = None
    legal_name: Optional[str] = None
    gst_no: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    mobile: Optional[str] = None


class ContactUpdate(BaseModel):
    contact_name: Optional[str] = None
    company_name: Optional[str] = None
    contact_type: Optional[str] = None
    customer_sub_type: Optional[str] = None
    website: Optional[str] = None
    language_code: Optional[str] = None
    credit_limit: Optional[float] = None
    contact_number: Optional[str] = None
    currency_id: Optional[UUID] = None
    payment_terms: Optional[int] = None
    payment_terms_label: Optional[str] = None
    notes: Optional[str] = None
    billing_address: Optional[dict] = None
    shipping_address: Optional[dict] = None
    default_templates: Optional[dict] = None
    custom_fields: Optional[list] = None
    tags: Optional[list[str]] = None
    is_portal_enabled: Optional[bool] = None
    vat_reg_no: Optional[str] = None
    tax_reg_no: Optional[str] = None
    country_code: Optional[str] = None
    vat_treatment: Optional[str] = None
    tax_treatment: Optional[str] = None
    tax_regime: Optional[str] = None
    legal_name: Optional[str] = None
    gst_no: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    mobile: Optional[str] = None


class AddressCreate(BaseModel):
    attention: Optional[str] = None
    address: Optional[str] = None
    street2: Optional[str] = None
    city: Optional[str] = None
    state_code: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    country: Optional[str] = None
    country_code: Optional[str] = None
    phone: Optional[str] = None
    fax: Optional[str] = None


class AddressUpdate(BaseModel):
    attention: Optional[str] = None
    address: Optional[str] = None
    street2: Optional[str] = None
    city: Optional[str] = None
    state_code: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    country: Optional[str] = None
    country_code: Optional[str] = None
    phone: Optional[str] = None
    fax: Optional[str] = None


class EmailInput(BaseModel):
    to_mail_ids: list[str]
    cc_mail_ids: Optional[list[str]] = None
    subject: str
    body: str


class CommentInput(BaseModel):
    description: str


@router.get("")
async def list_contacts(
    user: UserContext = Depends(_auth),
    contact_type: Optional[str] = Query(None),
    contact_name: Optional[str] = Query(None),
    company_name: Optional[str] = Query(None),
    email: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    return await acc_list(
        TABLE, user,
        filters={"contact_type": contact_type, "status": status},
        search_fields={"contact_name": contact_name, "company_name": company_name, "email": email},
        page=page, per_page=per_page,
    )


@router.post("")
async def create_contact(body: ContactCreate, user: UserContext = Depends(_auth)):
    return await acc_create(TABLE, body.model_dump(exclude_none=True), user)


@router.get("/{contact_id}")
async def get_contact(contact_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_get(TABLE, PK, contact_id, user, LABEL)


@router.put("/{contact_id}")
async def update_contact(contact_id: UUID, body: ContactUpdate, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, contact_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{contact_id}")
async def delete_contact(contact_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_delete(TABLE, PK, contact_id, user, LABEL)


@router.post("/{contact_id}/active")
async def mark_active(contact_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, contact_id, "active", user, LABEL)


@router.post("/{contact_id}/inactive")
async def mark_inactive(contact_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_status_update(TABLE, PK, contact_id, "inactive", user, LABEL)


# ── 1. Update contact using custom field ─────────────────────────────
@router.put("/contacts")
async def update_contact_custom_field(body: ContactUpdate, user: UserContext = Depends(_auth)):
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    contact_name = data.pop("contact_name", None)
    if not contact_name:
        raise HTTPException(status_code=400, detail="contact_name is required to identify the contact")
    from app.core.database import get_connection
    async with get_connection() as conn:
        if user.is_branch_user:
            row = await conn.fetchrow(
                f"SELECT {PK} FROM {TABLE} WHERE contact_name=$1 AND user_id=$2 AND branch_id=$3",
                contact_name, user.owner_id, user.branch_id,
            )
        else:
            row = await conn.fetchrow(
                f"SELECT {PK} FROM {TABLE} WHERE contact_name=$1 AND user_id=$2",
                contact_name, user.user_id,
            )
    if not row:
        raise HTTPException(status_code=404, detail="Contact not found")
    return await acc_update(TABLE, PK, row[PK], data, user, LABEL)


# ── 2. Enable portal ─────────────────────────────────────────────────
@router.post("/{contact_id}/portal/enable")
async def enable_portal(contact_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, contact_id, {"portal_enabled": True}, user, LABEL)


# ── 3. Enable payment reminder ───────────────────────────────────────
@router.post("/{contact_id}/paymentreminder/enable")
async def enable_payment_reminder(contact_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, contact_id, {"payment_reminder_enabled": True}, user, LABEL)


# ── 4. Disable payment reminder ──────────────────────────────────────
@router.post("/{contact_id}/paymentreminder/disable")
async def disable_payment_reminder(contact_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, contact_id, {"payment_reminder_enabled": False}, user, LABEL)


# ── 5. Get statement email content ───────────────────────────────────
@router.get("/{contact_id}/statements/email")
async def get_statement_email(contact_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_email_get(TABLE, PK, contact_id, user, LABEL, email_type="statement")


# ── 6. Email statement ───────────────────────────────────────────────
@router.post("/{contact_id}/statements/email")
async def send_statement_email(contact_id: UUID, body: EmailInput, user: UserContext = Depends(_auth)):
    return await acc_email_send(TABLE, PK, contact_id, body.model_dump(), user, LABEL, email_type="statement")


# ── 7. Email contact ─────────────────────────────────────────────────
@router.post("/{contact_id}/email")
async def email_contact(contact_id: UUID, body: EmailInput, user: UserContext = Depends(_auth)):
    return await acc_email_send(TABLE, PK, contact_id, body.model_dump(), user, LABEL, email_type="contact")


# ── 8. List comments ─────────────────────────────────────────────────
@router.get("/{contact_id}/comments")
async def list_comments(contact_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_comments_list(TABLE, PK, contact_id, user, LABEL)


# ── 9. List contact addresses ────────────────────────────────────────
@router.get("/{contact_id}/address")
async def list_addresses(contact_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_list(SUB_TABLE, PARENT_COL, contact_id, user)


# ── 10. Add contact address ──────────────────────────────────────────
@router.post("/{contact_id}/address")
async def create_address(contact_id: UUID, body: AddressCreate, user: UserContext = Depends(_auth)):
    return await acc_sub_create(SUB_TABLE, PARENT_COL, contact_id, body.model_dump(exclude_none=True), user)


# ── 11. Update address ───────────────────────────────────────────────
@router.put("/{contact_id}/address/{address_id}")
async def update_address(contact_id: UUID, address_id: UUID, body: AddressUpdate, user: UserContext = Depends(_auth)):
    return await acc_sub_update(SUB_TABLE, SUB_PK, address_id, PARENT_COL, contact_id, body.model_dump(exclude_unset=True, exclude_none=True), user)


# ── 12. Delete address ───────────────────────────────────────────────
@router.delete("/{contact_id}/address/{address_id}")
async def delete_address(contact_id: UUID, address_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_sub_delete(SUB_TABLE, SUB_PK, address_id, PARENT_COL, contact_id, user)


# ── 13. List contact refunds (stub) ──────────────────────────────────
@router.get("/{contact_id}/refunds")
async def list_refunds(contact_id: UUID, user: UserContext = Depends(_auth)):
    await acc_get(TABLE, PK, contact_id, user, LABEL)
    return {"message": "Contact refunds endpoint — join acc_creditnote_refunds with acc_credit_notes not yet implemented", "items": []}


# ── 14. Track 1099 ───────────────────────────────────────────────────
@router.post("/{contact_id}/track1099")
async def track_1099(contact_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, contact_id, {"track_1099": True}, user, LABEL)


# ── 15. Untrack 1099 ─────────────────────────────────────────────────
@router.post("/{contact_id}/untrack1099")
async def untrack_1099(contact_id: UUID, user: UserContext = Depends(_auth)):
    return await acc_update(TABLE, PK, contact_id, {"track_1099": False}, user, LABEL)


# ── 16. Unused retainer payments (stub) ──────────────────────────────
@router.get("/{contact_id}/receivables/unusedretainerpayments")
async def unused_retainer_payments(contact_id: UUID, user: UserContext = Depends(_auth)):
    await acc_get(TABLE, PK, contact_id, user, LABEL)
    return {"message": "Unused retainer payments not yet implemented", "items": []}
