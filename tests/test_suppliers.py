"""Unit + integration tests for the Supplier Management module.

Uses the same mock-connection pattern as the rest of the suite (no live DB):
`get_connection` in the service is patched with an AsyncMock context manager.
"""
import pytest
from unittest.mock import AsyncMock, patch
from decimal import Decimal

from pydantic import ValidationError as PydanticValidationError

from app.core.auth import UserContext
from app.core.exceptions import NotFoundError, ValidationError
from app.services.supplier_service import SupplierService
from app.api.v1.suppliers import SupplierCreate, SupplierPatch


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _owner() -> UserContext:
    return UserContext(
        user_id="owner-1",
        email="owner@test.com",
        role="owner",
        restaurant_id="rest-1",
        branch_id=None,
        owner_id="owner-1",
        is_branch_user=False,
    )


def _conn_cm(conn):
    """Wrap an AsyncMock connection in an async-context-manager mock."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _sample_row(**over) -> dict:
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "user_id": "owner-1",
        "restaurant_id": "rest-1",
        "branch_id": None,
        "supplier_code": "SUP-000001",
        "supplier_name": "Fresh Farms",
        "mobile": "9876543210",
        "status": "active",
        "payment_terms": "credit",
        "opening_balance": Decimal("100.00"),
        "current_outstanding": Decimal("100.00"),
        "created_at": "2026-07-05T00:00:00+00:00",
        "updated_at": "2026-07-05T00:00:00+00:00",
        "deleted_at": None,
    }
    row.update(over)
    return row


# ─────────────────────────────────────────────────────────────
# Schema / validation
# ─────────────────────────────────────────────────────────────

class TestSupplierValidation:

    def test_minimal_valid_payload(self):
        m = SupplierCreate(supplier_name="Fresh Farms", mobile="9876543210")
        assert m.supplier_name == "Fresh Farms"
        assert m.mobile == "9876543210"

    def test_supplier_name_required(self):
        with pytest.raises(PydanticValidationError):
            SupplierCreate(mobile="9876543210")

    def test_mobile_required(self):
        with pytest.raises(PydanticValidationError):
            SupplierCreate(supplier_name="Fresh Farms")

    def test_mobile_normalises_plus91_prefix(self):
        m = SupplierCreate(supplier_name="X", mobile="+91 98765 43210")
        assert m.mobile == "9876543210"

    def test_invalid_mobile_rejected(self):
        with pytest.raises(PydanticValidationError):
            SupplierCreate(supplier_name="X", mobile="12345")

    def test_gstin_uppercased_and_validated(self):
        m = SupplierCreate(
            supplier_name="X", mobile="9876543210",
            gstin="27aapfu0939f1zv",
        )
        assert m.gstin == "27AAPFU0939F1ZV"

    def test_invalid_gstin_rejected(self):
        with pytest.raises(PydanticValidationError):
            SupplierCreate(supplier_name="X", mobile="9876543210", gstin="NOTAGSTIN")

    def test_invalid_pan_rejected(self):
        with pytest.raises(PydanticValidationError):
            SupplierCreate(supplier_name="X", mobile="9876543210", pan="ABC")

    def test_invalid_ifsc_rejected(self):
        with pytest.raises(PydanticValidationError):
            SupplierCreate(supplier_name="X", mobile="9876543210", ifsc="BAD")

    def test_invalid_email_rejected(self):
        with pytest.raises(PydanticValidationError):
            SupplierCreate(supplier_name="X", mobile="9876543210", email="not-an-email")

    def test_invalid_category_rejected(self):
        with pytest.raises(PydanticValidationError):
            SupplierCreate(supplier_name="X", mobile="9876543210", category="widgets")

    def test_invalid_payment_terms_rejected(self):
        with pytest.raises(PydanticValidationError):
            SupplierCreate(supplier_name="X", mobile="9876543210", payment_terms="barter")

    def test_negative_credit_limit_rejected(self):
        with pytest.raises(PydanticValidationError):
            SupplierCreate(supplier_name="X", mobile="9876543210", credit_limit=Decimal("-5"))

    def test_patch_allows_empty(self):
        m = SupplierPatch()
        assert m.model_dump(exclude_unset=True) == {}


# ─────────────────────────────────────────────────────────────
# Service — create
# ─────────────────────────────────────────────────────────────

class TestCreateSupplier:

    @pytest.mark.asyncio
    async def test_create_returns_row_and_sets_outstanding_to_opening(self):
        svc = SupplierService()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=_sample_row())
        with patch("app.services.supplier_service.get_connection", return_value=_conn_cm(conn)):
            out = await svc.create_supplier(_owner(), {
                "supplier_name": "Fresh Farms",
                "mobile": "9876543210",
                "opening_balance": Decimal("100.00"),
            })
        assert out["supplier_code"] == "SUP-000001"
        # opening_balance mirrored into current_outstanding in the INSERT params
        args = conn.fetchrow.await_args.args
        assert "INSERT INTO suppliers" in args[0]

    @pytest.mark.asyncio
    async def test_create_requires_name(self):
        svc = SupplierService()
        with pytest.raises(ValidationError):
            await svc.create_supplier(_owner(), {"mobile": "9876543210"})

    @pytest.mark.asyncio
    async def test_create_rejects_bad_status(self):
        svc = SupplierService()
        with pytest.raises(ValidationError):
            await svc.create_supplier(_owner(), {
                "supplier_name": "X", "mobile": "9876543210", "status": "archived",
            })


# ─────────────────────────────────────────────────────────────
# Service — read
# ─────────────────────────────────────────────────────────────

class TestReadSupplier:

    @pytest.mark.asyncio
    async def test_get_missing_raises_not_found(self):
        svc = SupplierService()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        with patch("app.services.supplier_service.get_connection", return_value=_conn_cm(conn)):
            with pytest.raises(NotFoundError):
                await svc.get_supplier(_owner(), "missing-id")

    @pytest.mark.asyncio
    async def test_list_returns_pagination_envelope(self):
        svc = SupplierService()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=1)
        conn.fetch = AsyncMock(return_value=[_sample_row()])
        with patch("app.services.supplier_service.get_connection", return_value=_conn_cm(conn)):
            out = await svc.list_suppliers(_owner(), limit=25, offset=0)
        assert out["total"] == 1
        assert out["limit"] == 25
        assert out["offset"] == 0
        assert len(out["items"]) == 1
        assert out["items"][0]["supplier_code"] == "SUP-000001"

    @pytest.mark.asyncio
    async def test_list_search_adds_filter_without_error(self):
        svc = SupplierService()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=0)
        conn.fetch = AsyncMock(return_value=[])
        with patch("app.services.supplier_service.get_connection", return_value=_conn_cm(conn)):
            out = await svc.list_suppliers(
                _owner(), status="active", gstin="27AAPFU0939F1ZV",
                search="fresh", limit=10, offset=5,
            )
        assert out["total"] == 0
        # count query used the same WHERE as the rows query
        count_sql = conn.fetchval.await_args.args[0]
        assert "ILIKE" in count_sql and "upper(gstin)" in count_sql


# ─────────────────────────────────────────────────────────────
# Service — update / delete
# ─────────────────────────────────────────────────────────────

class TestUpdateDeleteSupplier:

    @pytest.mark.asyncio
    async def test_update_no_fields_raises(self):
        svc = SupplierService()
        with pytest.raises(ValidationError):
            await svc.update_supplier(_owner(), "id-1", {})

    @pytest.mark.asyncio
    async def test_update_ignores_non_whitelisted_columns(self):
        svc = SupplierService()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=_sample_row(city="Pune"))
        with patch("app.services.supplier_service.get_connection", return_value=_conn_cm(conn)):
            # current_outstanding / supplier_code / user_id must be ignored,
            # only `city` is applied.
            out = await svc.update_supplier(_owner(), "id-1", {
                "city": "Pune",
                "current_outstanding": Decimal("9999"),
                "supplier_code": "HACK",
                "user_id": "other",
            })
        assert out["city"] == "Pune"
        sql = conn.fetchrow.await_args.args[0]
        assert "current_outstanding" not in sql
        assert "supplier_code" not in sql

    @pytest.mark.asyncio
    async def test_update_missing_raises_not_found(self):
        svc = SupplierService()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        with patch("app.services.supplier_service.get_connection", return_value=_conn_cm(conn)):
            with pytest.raises(NotFoundError):
                await svc.update_supplier(_owner(), "id-1", {"city": "Pune"})

    @pytest.mark.asyncio
    async def test_delete_soft_deletes(self):
        svc = SupplierService()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"id": "id-1"})
        with patch("app.services.supplier_service.get_connection", return_value=_conn_cm(conn)):
            out = await svc.delete_supplier(_owner(), "id-1")
        assert out == {"deleted": True, "id": "id-1"}
        sql = conn.fetchrow.await_args.args[0]
        assert "deleted_at = NOW()" in sql

    @pytest.mark.asyncio
    async def test_delete_missing_raises_not_found(self):
        svc = SupplierService()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        with patch("app.services.supplier_service.get_connection", return_value=_conn_cm(conn)):
            with pytest.raises(NotFoundError):
                await svc.delete_supplier(_owner(), "id-1")
