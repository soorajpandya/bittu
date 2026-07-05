"""Supplier Management endpoints — CRUD for the procurement `suppliers` master.

Routes (all tenant-scoped, RBAC-gated):
    POST   /suppliers            create              (supplier.write)
    GET    /suppliers            list + search       (supplier.read)
    GET    /suppliers/{id}       retrieve            (supplier.read)
    PUT    /suppliers/{id}       full replace        (supplier.write)
    PATCH  /suppliers/{id}       partial update      (supplier.write)
    DELETE /suppliers/{id}       soft delete         (supplier.delete)

`supplier_code` is auto-generated server-side and is read-only.
`current_outstanding` is calculated and is never accepted from the client.
"""
from typing import Optional
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator

from app.core.auth import UserContext, require_permission
from app.services.supplier_service import SupplierService

router = APIRouter(prefix="/suppliers", tags=["Suppliers"])
_svc = SupplierService()


# ─────────────────────────────────────────────────────────────────────
# Field-format validators (India). Only enforced when a value is present.
# ─────────────────────────────────────────────────────────────────────

_GSTIN_RE = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"
_PAN_RE = r"^[A-Z]{5}[0-9]{4}[A-Z]$"
_IFSC_RE = r"^[A-Z]{4}0[A-Z0-9]{6}$"
_PINCODE_RE = r"^[1-9][0-9]{5}$"
_MOBILE_RE = r"^[6-9][0-9]{9}$"
_EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

VALID_CATEGORIES = {
    "vegetables", "dairy", "meat_poultry", "seafood", "grocery",
    "beverages", "packaging", "cleaning_supplies", "other",
}


def _blank_to_none(v):
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _norm_mobile(v: Optional[str]) -> Optional[str]:
    """Strip spaces/dashes and a leading +91/91/0, then validate 10-digit."""
    v = _blank_to_none(v)
    if v is None:
        return None
    digits = "".join(ch for ch in v if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    import re
    if not re.match(_MOBILE_RE, digits):
        raise ValueError("must be a valid 10-digit Indian mobile number")
    return digits


class SupplierBase(BaseModel):
    business_name: Optional[str] = Field(None, max_length=200)
    contact_person: Optional[str] = Field(None, max_length=120)
    alternate_phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=200)

    address_line1: Optional[str] = Field(None, max_length=200)
    address_line2: Optional[str] = Field(None, max_length=200)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    country: Optional[str] = Field(None, max_length=100)
    pincode: Optional[str] = None

    gstin: Optional[str] = None
    pan: Optional[str] = None
    fssai_license: Optional[str] = Field(None, max_length=30)

    category: Optional[str] = None
    min_order_value: Optional[Decimal] = Field(None, ge=0)
    lead_time_days: Optional[int] = Field(None, ge=0, le=365)
    is_preferred: Optional[bool] = None

    payment_terms: Optional[str] = None
    credit_period_days: Optional[int] = Field(None, ge=0, le=365)
    credit_limit: Optional[Decimal] = Field(None, ge=0)
    opening_balance: Optional[Decimal] = None

    bank_name: Optional[str] = Field(None, max_length=120)
    account_number: Optional[str] = Field(None, max_length=34)
    ifsc: Optional[str] = None
    upi_id: Optional[str] = Field(None, max_length=100)

    status: Optional[str] = None
    notes: Optional[str] = Field(None, max_length=2000)

    @field_validator("email", mode="before")
    @classmethod
    def _v_email(cls, v):
        v = _blank_to_none(v)
        if v is None:
            return None
        import re
        if not re.match(_EMAIL_RE, v):
            raise ValueError("invalid email address")
        return v.lower()

    @field_validator("gstin", mode="before")
    @classmethod
    def _v_gstin(cls, v):
        v = _blank_to_none(v)
        if v is None:
            return None
        v = v.upper()
        import re
        if not re.match(_GSTIN_RE, v):
            raise ValueError("invalid GSTIN")
        return v

    @field_validator("pan", mode="before")
    @classmethod
    def _v_pan(cls, v):
        v = _blank_to_none(v)
        if v is None:
            return None
        v = v.upper()
        import re
        if not re.match(_PAN_RE, v):
            raise ValueError("invalid PAN")
        return v

    @field_validator("ifsc", mode="before")
    @classmethod
    def _v_ifsc(cls, v):
        v = _blank_to_none(v)
        if v is None:
            return None
        v = v.upper()
        import re
        if not re.match(_IFSC_RE, v):
            raise ValueError("invalid IFSC code")
        return v

    @field_validator("pincode", mode="before")
    @classmethod
    def _v_pincode(cls, v):
        v = _blank_to_none(v)
        if v is None:
            return None
        import re
        if not re.match(_PINCODE_RE, str(v)):
            raise ValueError("invalid 6-digit PIN code")
        return str(v)

    @field_validator("alternate_phone", mode="before")
    @classmethod
    def _v_alt_phone(cls, v):
        return _norm_mobile(v)

    @field_validator("category", mode="before")
    @classmethod
    def _v_category(cls, v):
        v = _blank_to_none(v)
        if v is None:
            return None
        v = v.lower()
        if v not in VALID_CATEGORIES:
            raise ValueError(f"category must be one of: {', '.join(sorted(VALID_CATEGORIES))}")
        return v

    @field_validator("payment_terms", mode="before")
    @classmethod
    def _v_terms(cls, v):
        v = _blank_to_none(v)
        if v is None:
            return None
        v = v.lower()
        if v not in {"cash", "credit", "advance"}:
            raise ValueError("payment_terms must be one of: cash, credit, advance")
        return v

    @field_validator("status", mode="before")
    @classmethod
    def _v_status(cls, v):
        v = _blank_to_none(v)
        if v is None:
            return None
        v = v.lower()
        if v not in {"active", "inactive"}:
            raise ValueError("status must be one of: active, inactive")
        return v


class SupplierCreate(SupplierBase):
    supplier_name: str = Field(..., min_length=1, max_length=200)
    mobile: str

    @field_validator("supplier_name", mode="before")
    @classmethod
    def _v_name(cls, v):
        v = _blank_to_none(v)
        if v is None:
            raise ValueError("supplier_name is required")
        return v

    @field_validator("mobile", mode="before")
    @classmethod
    def _v_mobile(cls, v):
        out = _norm_mobile(v)
        if out is None:
            raise ValueError("mobile is required")
        return out


class SupplierReplace(SupplierCreate):
    """PUT — full replacement; same required fields as create."""


class SupplierPatch(SupplierBase):
    """PATCH — everything optional."""
    supplier_name: Optional[str] = Field(None, min_length=1, max_length=200)
    mobile: Optional[str] = None
    branch_id: Optional[str] = None

    @field_validator("supplier_name", mode="before")
    @classmethod
    def _v_name(cls, v):
        v = _blank_to_none(v)
        if v is None:
            return None
        return v

    @field_validator("mobile", mode="before")
    @classmethod
    def _v_mobile(cls, v):
        return _norm_mobile(v)


# ─────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_supplier(
    body: SupplierCreate,
    user: UserContext = Depends(require_permission("supplier.write")),
):
    supplier = await _svc.create_supplier(user, body.model_dump())
    return {"supplier": supplier, "created": True}


@router.get("")
async def list_suppliers(
    status: Optional[str] = Query(None, pattern="^(active|inactive)$"),
    supplier_name: Optional[str] = None,
    mobile: Optional[str] = None,
    gstin: Optional[str] = None,
    search: Optional[str] = Query(None, description="Free-text search across name/contact/mobile/email/code"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("supplier.read")),
):
    return await _svc.list_suppliers(
        user, status=status, supplier_name=supplier_name,
        mobile=mobile, gstin=gstin, search=search, limit=limit, offset=offset,
    )


@router.get("/{supplier_id}")
async def get_supplier(
    supplier_id: str,
    user: UserContext = Depends(require_permission("supplier.read")),
):
    return {"supplier": await _svc.get_supplier(user, supplier_id)}


@router.put("/{supplier_id}")
async def replace_supplier(
    supplier_id: str,
    body: SupplierReplace,
    user: UserContext = Depends(require_permission("supplier.write")),
):
    # PUT = full replace: model_dump() (no exclude_unset) resets omitted
    # optionals to their defaults / NULL.
    supplier = await _svc.update_supplier(user, supplier_id, body.model_dump())
    return {"supplier": supplier}


@router.patch("/{supplier_id}")
async def update_supplier(
    supplier_id: str,
    body: SupplierPatch,
    user: UserContext = Depends(require_permission("supplier.write")),
):
    supplier = await _svc.update_supplier(user, supplier_id, body.model_dump(exclude_unset=True))
    return {"supplier": supplier}


@router.delete("/{supplier_id}", status_code=200)
async def delete_supplier(
    supplier_id: str,
    user: UserContext = Depends(require_permission("supplier.delete")),
):
    return await _svc.delete_supplier(user, supplier_id)
