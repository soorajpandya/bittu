"""
Razorpay Route REST API (Phase 7 — linked accounts + transfers).

Prefix ``/razorpay-route`` deliberately avoids any clash with the legacy
``/razorpay`` namespace. All gateway side-effects funnel through
``rzp_route_service`` so idempotency keys and merchant resolution stay in
exactly one place.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.services.kyc_service import kyc_service
from app.services.razorpay.route_service import rzp_route_service

logger = get_logger(__name__)

router = APIRouter(prefix="/razorpay-route", tags=["Razorpay Route"])


def _mid(user: UserContext) -> str:
    if not user.restaurant_id:
        raise HTTPException(status_code=400, detail="restaurant_id required")
    return str(user.restaurant_id)


# ── Models ────────────────────────────────────────────────────────────────


class ProvisionLinkedAccountIn(BaseModel):
    bank_account_number: Optional[str] = Field(
        None,
        description="Full account number — used in-memory only. Stored as last4+sha256.",
    )
    ifsc: Optional[str] = None
    beneficiary_name: Optional[str] = Field(None, min_length=1, max_length=255)
    reference_id: Optional[str] = Field(
        None,
        min_length=3,
        max_length=20,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="3..20 chars of [A-Za-z0-9_-]. Requires the `route_code_support` feature on the platform account; ignored automatically if the feature is disabled.",
    )
    notes: Optional[dict[str, Any]] = None

    # Razorpay /v2/accounts profile fields — forwarded verbatim.
    category: Optional[str] = Field(
        None, description="Razorpay profile.category (defaults to 'food')"
    )
    subcategory: Optional[str] = Field(
        None, description="Razorpay profile.subcategory (defaults to 'restaurant')"
    )
    addresses: Optional[dict[str, Any]] = None

    # Razorpay /v2/accounts optional top-level fields.
    customer_facing_business_name: Optional[str] = Field(
        None, min_length=1, max_length=255,
        description="DBA name shown to customers. Defaults to legal_business_name on Razorpay's side if omitted.",
    )
    contact_info: Optional[dict[str, Any]] = Field(
        None,
        description="Contact details by type: {chargeback: {email, phone, policy_url}, refund: {...}, support: {...}}.",
    )
    apps: Optional[dict[str, Any]] = Field(
        None,
        description="Account apps; typically {websites: [\"https://example.com\"]}.",
    )


class CreateTransferIn(BaseModel):
    razorpay_payment_id: str = Field(..., min_length=4)
    amount_paise: int = Field(..., ge=100)
    currency: str = Field("INR", min_length=3, max_length=3)
    on_hold: bool = False
    on_hold_until_epoch: Optional[int] = None
    notes: Optional[dict[str, Any]] = None


class ReverseTransferIn(BaseModel):
    amount_paise: Optional[int] = Field(None, ge=100)
    notes: Optional[dict[str, Any]] = None


class CreateStakeholderIn(BaseModel):
    relationship: Optional[dict[str, Any]] = None
    kyc: Optional[dict[str, Any]] = None
    addresses: Optional[dict[str, Any]] = None


class RequestProductIn(BaseModel):
    tnc_accepted: bool = True


class UpdateProductBankIn(BaseModel):
    bank_account_number: str = Field(..., min_length=4)
    ifsc: str = Field(..., min_length=4, max_length=20)
    beneficiary_name: Optional[str] = None
    tnc_accepted: bool = True


class UpdateLinkedAccountIn(BaseModel):
    """Razorpay ``PATCH /v2/accounts/:account_id`` — every field
    optional. ``business_type`` and ``email`` cannot be updated per
    Razorpay spec and are deliberately absent here."""

    phone: Optional[str] = Field(None, min_length=8, max_length=15)
    legal_business_name: Optional[str] = Field(None, min_length=4, max_length=200)
    customer_facing_business_name: Optional[str] = Field(None, min_length=1, max_length=255)
    reference_id: Optional[str] = Field(
        None,
        min_length=1,
        max_length=512,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="1..512 chars of [A-Za-z0-9_-] per update spec. Dropped automatically if the platform lacks `route_code_support`.",
    )
    contact_name: Optional[str] = Field(None, min_length=4, max_length=255)
    notes: Optional[dict[str, Any]] = None

    # profile.*
    category: Optional[str] = None
    subcategory: Optional[str] = None
    business_model: Optional[str] = Field(None, min_length=1, max_length=255)
    addresses: Optional[dict[str, Any]] = Field(
        None,
        description="{registered: {...}, operation: {...}} — both slots optional.",
    )

    # legal_info.* — silently dropped server-side if regex fails.
    pan: Optional[str] = Field(None, pattern=r"^[A-Za-z]{5}\d{4}[A-Za-z]$")
    gst: Optional[str] = Field(
        None,
        pattern=r"^[0-3][0-9][A-Za-z]{5}[0-9]{4}[A-Za-z][0-9][A-Za-z0-9]{2}$",
    )

    contact_info: Optional[dict[str, Any]] = None
    apps: Optional[dict[str, Any]] = None


class OnboardIn(BaseModel):
    bank_account_number: str = Field(..., min_length=4)
    ifsc: Optional[str] = None
    beneficiary_name: Optional[str] = Field(None, min_length=1, max_length=255)
    reference_id: Optional[str] = Field(
        None,
        min_length=3,
        max_length=20,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="3..20 chars of [A-Za-z0-9_-]. Requires the `route_code_support` feature on the platform account; ignored automatically if the feature is disabled.",
    )
    tnc_accepted: bool = True
    notes: Optional[dict[str, Any]] = None

    # ── Optional KYC profile fields ─────────────────────────────────
    # If supplied, the onboard handler upserts these into merchant_kyc_*
    # tables before provisioning, so FE doesn't have to call a separate
    # KYC endpoint. All fields are optional; the handler only writes
    # the ones the caller provided.
    legal_name: Optional[str] = Field(None, min_length=4, max_length=200)
    business_type: Optional[str] = Field(
        None,
        description="proprietorship|partnership|llp|private_limited|public_limited|huf|trust|society|individual|other",
    )
    pan: Optional[str] = Field(None, pattern=r"^[A-Za-z]{5}\d{4}[A-Za-z]$")
    gstin: Optional[str] = Field(
        None,
        pattern=r"^[0-3][0-9][A-Za-z]{5}[0-9]{4}[A-Za-z][0-9][A-Za-z0-9]{2}$",
    )
    contact_email: Optional[str] = Field(None, min_length=3, max_length=254)
    contact_phone: Optional[str] = Field(None, min_length=8, max_length=15)
    registered_address: Optional[dict[str, Any]] = None

    owner_name: Optional[str] = None
    owner_role: Optional[str] = Field(
        None,
        description="director|partner|proprietor|ubo|authorized_signatory",
    )
    owner_email: Optional[str] = None
    owner_phone: Optional[str] = None
    owner_pan: Optional[str] = Field(None, pattern=r"^[A-Za-z]{5}\d{4}[A-Za-z]$")
    owner_dob: Optional[str] = Field(None, description="YYYY-MM-DD")
    owner_ownership_pct: Optional[float] = Field(None, ge=0, le=100)

    bank_name: Optional[str] = None
    account_type: Optional[str] = Field(
        None, description="savings|current|nro|nre"
    )

    # ── Razorpay /v2/accounts profile fields ────────────────────────
    # Forwarded verbatim to Razorpay's create-linked-account call.
    # `addresses` is the full Razorpay-shaped dict (e.g.
    # {"registered": {"street1": ..., "city": ..., ...}}). When omitted
    # the handler builds it from registered_address above.
    category: Optional[str] = Field(
        None, description="Razorpay profile.category (defaults to 'food')"
    )
    subcategory: Optional[str] = Field(
        None, description="Razorpay profile.subcategory (defaults to 'restaurant')"
    )
    addresses: Optional[dict[str, Any]] = None

    # Razorpay /v2/accounts optional top-level fields (forwarded verbatim).
    customer_facing_business_name: Optional[str] = Field(
        None, min_length=1, max_length=255,
        description="DBA name shown to customers. Defaults to legal_business_name on Razorpay's side if omitted.",
    )
    contact_info: Optional[dict[str, Any]] = Field(
        None,
        description="Contact details by type: {chargeback: {email, phone, policy_url}, refund: {...}, support: {...}}.",
    )
    apps: Optional[dict[str, Any]] = Field(
        None,
        description="Account apps; typically {websites: [\"https://example.com\"]}.",
    )


# ── Linked account ────────────────────────────────────────────────────────


@router.get("/linked-account")
async def get_linked_account(
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await rzp_route_service.get_linked_account(merchant_id=_mid(user))


@router.post("/linked-account/provision")
async def provision_linked_account(
    body: ProvisionLinkedAccountIn = Body(default_factory=ProvisionLinkedAccountIn),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    """
    Idempotent: if a linked account already exists for this merchant we
    just resync state from Razorpay rather than creating a second one.
    """
    try:
        return await rzp_route_service.provision_linked_account(
            merchant_id=_mid(user),
            bank_account_number=body.bank_account_number,
            ifsc_override=body.ifsc,
            beneficiary_name_override=body.beneficiary_name,
            reference_id=body.reference_id,
            extra_notes=body.notes,
            category=body.category,
            subcategory=body.subcategory,
            addresses_override=body.addresses,
            customer_facing_business_name=body.customer_facing_business_name,
            contact_info=body.contact_info,
            apps=body.apps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/linked-account/sync")
async def sync_linked_account(
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    try:
        return await rzp_route_service.sync_linked_account(merchant_id=_mid(user))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.patch("/linked-account")
async def update_linked_account(
    body: UpdateLinkedAccountIn = Body(...),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    """Merchant-driven PATCH of the Razorpay linked account.

    Mirrors ``PATCH /v2/accounts/:account_id``. Send only the fields you
    want to change; omitted fields are left untouched on the gateway.
    ``business_type`` and ``email`` cannot be updated.
    """
    try:
        return await rzp_route_service.update_linked_account_details(
            merchant_id=_mid(user),
            phone=body.phone,
            legal_business_name=body.legal_business_name,
            customer_facing_business_name=body.customer_facing_business_name,
            reference_id=body.reference_id,
            contact_name=body.contact_name,
            notes=body.notes,
            category=body.category,
            subcategory=body.subcategory,
            business_model=body.business_model,
            addresses=body.addresses,
            pan=body.pan,
            gst=body.gst,
            contact_info=body.contact_info,
            apps=body.apps,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Stakeholder (Route onboarding step 3) ─────────────────────────────────


@router.post("/linked-account/stakeholder")
async def create_stakeholder(
    body: CreateStakeholderIn = Body(default_factory=CreateStakeholderIn),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    try:
        return await rzp_route_service.create_stakeholder_for_merchant(
            merchant_id=_mid(user),
            relationship_overrides=body.relationship,
            kyc_overrides=body.kyc,
            addresses_overrides=body.addresses,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Product configuration (Route onboarding steps 4 & 5) ─────────────────


@router.post("/linked-account/product")
async def request_product(
    body: RequestProductIn = Body(default_factory=RequestProductIn),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    try:
        return await rzp_route_service.request_route_product(
            merchant_id=_mid(user), tnc_accepted=body.tnc_accepted,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/linked-account/product")
async def update_product_bank(
    body: UpdateProductBankIn,
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    try:
        return await rzp_route_service.update_route_product_with_bank(
            merchant_id=_mid(user),
            bank_account_number=body.bank_account_number,
            ifsc=body.ifsc,
            beneficiary_name=body.beneficiary_name,
            tnc_accepted=body.tnc_accepted,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/linked-account/product/sync")
async def sync_product(
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    try:
        return await rzp_route_service.sync_route_product(merchant_id=_mid(user))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/linked-account/onboard")
async def onboard_route_merchant(
    body: OnboardIn,
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    """
    End-to-end Route onboarding: linked account → stakeholder → request
    product → update product with settlement bank details. Each step is
    independently idempotent so this endpoint is safe to retry.

    If KYC profile fields (legal_name, business_type, owner_name, …) are
    supplied in the request body, they are upserted into the
    ``merchant_kyc_*`` tables before provisioning. This lets the FE
    onboard a brand-new merchant in a single round-trip.
    """
    merchant_id = _mid(user)
    try:
        await _maybe_seed_kyc(merchant_id, body)
        return await rzp_route_service.onboard_route_merchant(
            merchant_id=merchant_id,
            bank_account_number=body.bank_account_number,
            ifsc=body.ifsc,
            beneficiary_name=body.beneficiary_name,
            reference_id=body.reference_id,
            tnc_accepted=body.tnc_accepted,
            extra_notes=body.notes,
            category=body.category,
            subcategory=body.subcategory,
            addresses_override=body.addresses,
            customer_facing_business_name=body.customer_facing_business_name,
            contact_info=body.contact_info,
            apps=body.apps,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


async def _maybe_seed_kyc(merchant_id: str, body: OnboardIn) -> None:
    """Upsert KYC profile / owner / bank from optional onboard body fields.

    Only writes when the caller supplied a value. Existing rows are not
    overwritten: owner and bank insertion are skipped if a primary
    signatory / primary bank already exists for the merchant.
    """
    # ── 1. Profile (always upsert; update_profile is a partial update) ─
    profile_fields = {
        k: v for k, v in {
            "legal_name":         body.legal_name,
            "business_type":      body.business_type,
            "pan":                body.pan,
            "gstin":              body.gstin,
            "contact_email":      body.contact_email,
            "contact_phone":      body.contact_phone,
            "registered_address": body.registered_address,
        }.items()
        if v is not None
    }
    if profile_fields:
        # get_or_create ensures the row exists; update_profile only writes
        # supplied fields and is a no-op when status != draft/rejected/suspended
        await kyc_service.get_or_create_profile(merchant_id)
        try:
            await kyc_service.update_profile(merchant_id, **profile_fields)
        except ConflictError:
            # profile already submitted/approved — leave it alone; the
            # caller probably just retried onboard with stale form data
            pass

    # ── 2. Primary signatory owner ─────────────────────────────────────
    if body.owner_name and body.owner_role:
        existing = await kyc_service.list_owners(merchant_id)
        has_signatory = any(o.get("is_signatory") for o in existing)
        if not has_signatory:
            await kyc_service.add_owner(
                merchant_id,
                full_name=body.owner_name,
                role=body.owner_role,
                email=body.owner_email or body.contact_email,
                phone=body.owner_phone or body.contact_phone,
                pan=body.owner_pan,
                dob=body.owner_dob,
                ownership_pct=body.owner_ownership_pct or 100.0,
                is_signatory=True,
            )

    # ── 3. Primary bank account (from the same bank fields used by Route) ─
    if body.bank_account_number and body.ifsc:
        existing_banks = await kyc_service.list_bank_accounts(merchant_id)
        has_primary = any(b.get("is_primary") for b in existing_banks)
        if not has_primary:
            try:
                await kyc_service.add_bank_account(
                    merchant_id,
                    account_holder_name=(
                        body.beneficiary_name
                        or body.owner_name
                        or body.legal_name
                        or "Account Holder"
                    ),
                    account_number=body.bank_account_number,
                    ifsc=body.ifsc,
                    bank_name=body.bank_name,
                    account_type=body.account_type or "current",
                    is_primary=True,
                )
            except ValidationError:
                # bank number/ifsc failed local validation — let the
                # provision step raise the canonical error so the FE
                # sees the same shape it does today
                pass


# ── Transfers ─────────────────────────────────────────────────────────────


@router.get("/transfers")
async def list_transfers(
    status: Optional[str] = Query(None, description="created|processed|reversed|failed"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await rzp_route_service.list_transfers(
        merchant_id=_mid(user), status=status, limit=limit, offset=offset,
    )


@router.get("/transfers/{transfer_id}")
async def get_transfer(
    transfer_id: str,
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    row = await rzp_route_service.get_transfer(
        merchant_id=_mid(user), transfer_id=transfer_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="transfer not found")
    return row


@router.post("/transfers")
async def create_transfer(
    body: CreateTransferIn,
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    try:
        return await rzp_route_service.create_transfer(
            merchant_id=_mid(user),
            razorpay_payment_id=body.razorpay_payment_id,
            amount_paise=body.amount_paise,
            currency=body.currency,
            on_hold=body.on_hold,
            on_hold_until_epoch=body.on_hold_until_epoch,
            notes=body.notes,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/transfers/{transfer_id}/reverse")
async def reverse_transfer(
    transfer_id: str,
    body: ReverseTransferIn = Body(default_factory=ReverseTransferIn),
    user: UserContext = Depends(require_permission("razorpay.route.write")),
):
    try:
        return await rzp_route_service.reverse_transfer(
            merchant_id=_mid(user),
            transfer_id=transfer_id,
            amount_paise=body.amount_paise,
            notes=body.notes,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/transfers/{transfer_id}/sync")
async def sync_transfer(
    transfer_id: str,
    user: UserContext = Depends(require_permission("razorpay.route.read")),
):
    return await rzp_route_service.sync_transfer(
        merchant_id=_mid(user), transfer_id=transfer_id,
    )
