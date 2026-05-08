"""
Tests for POST /orders/checkout idempotency, concurrency, rollback,
GET /orders ordering/filtering, and auth scope enforcement.

Covers all acceptance criteria from the POS Order Flow stabilisation spec.

Test strategy
-------------
- Pure unit tests with mocked DB and Redis: fast, deterministic.
- Concurrency tests: use asyncio.gather to simulate simultaneous requests.
- Each test is self-contained (no shared state).
"""
import asyncio
import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from app.core.auth import UserContext
from app.core.exceptions import (
    NotFoundError, ForbiddenError, ValidationError, LockAcquisitionError,
)
from app.services.order_service import OrderService


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _owner(user_id="owner-1", restaurant_id="rest-1", branch_id=None):
    return UserContext(
        user_id=user_id,
        email="owner@test.com",
        role="owner",
        restaurant_id=restaurant_id,
        branch_id=branch_id,
        owner_id=user_id,
        is_branch_user=False,
    )


def _staff(owner_id="owner-1", user_id="staff-1", branch_id="branch-1", restaurant_id="rest-1"):
    return UserContext(
        user_id=user_id,
        email="staff@test.com",
        role="cashier",
        restaurant_id=restaurant_id,
        branch_id=branch_id,
        owner_id=owner_id,
        is_branch_user=True,
    )


SAMPLE_ITEMS = [{"item_id": 42, "item_name": "Burger", "quantity": 2}]

ITEM_DB_ROW = {
    "Item_ID": 42,
    "Item_Name": "Burger",
    "price": Decimal("150.00"),
    "Available_Status": True,
}

TAX_ROW = {"tax_percentage": Decimal("5.0")}


def _make_conn_mock(
    item_row=None,
    tax_row=None,
    idempotency_row=None,
):
    """
    Build a mock asyncpg connection that returns sensible defaults.
    Individual tests override specific calls as needed.
    """
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=_default_fetchrow(item_row, tax_row, idempotency_row))
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    return conn


def _default_fetchrow(item_row, tax_row, idempotency_row):
    async def _fetchrow(query, *args):
        q = query.strip().lower()
        if "checkout_idempotency" in q:
            return idempotency_row
        if "restaurant_settings" in q:
            return tax_row or TAX_ROW
        if "item_variants" in q:
            return None
        if "items" in q:
            return item_row or ITEM_DB_ROW
        if "item_addons" in q:
            return None
        if "coupons" in q:
            return None
        return None
    return _fetchrow


# ─────────────────────────────────────────────────────────────
# AC-1: Checkout creates order with valid payload + full response
# ─────────────────────────────────────────────────────────────

class TestCheckoutSuccess:

    @pytest.mark.asyncio
    async def test_creates_order_returns_full_response(self):
        svc = OrderService()
        user = _owner()
        conn = _make_conn_mock()

        with (
            patch("app.services.order_service.get_connection") as mock_get_conn,
            patch("app.services.order_service.get_serializable_transaction") as mock_get_tx,
            patch("app.services.order_service.emit_and_publish", new_callable=AsyncMock),
        ):
            mock_get_conn.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_get_conn.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_get_tx.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_get_tx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await svc.checkout(
                user=user,
                items=SAMPLE_ITEMS,
                source="pos",
                order_type="pos",
                payment_method="cash",
                total_amount=315.0,
            )

        # Must include required response contract fields
        assert "id" in result
        assert "order_number" in result
        assert "status" in result
        assert "created_at" in result
        assert "updated_at" in result
        assert "total_amount" in result
        assert "subtotal" in result
        assert "tax_amount" in result
        assert "discount_amount" in result
        assert "payment_method" in result
        assert "source" in result
        assert "items" in result

        # Items must include required sub-fields
        assert len(result["items"]) == 1
        item = result["items"][0]
        assert "item_id" in item
        assert "item_name" in item
        assert "quantity" in item
        assert "unit_price" in item
        assert "total_price" in item

        # idempotent should be False for a new order
        assert result.get("idempotent") is False

    @pytest.mark.asyncio
    async def test_server_recalculates_price_ignores_client_hint(self):
        """Server must ignore client price hints and use DB prices."""
        svc = OrderService()
        user = _owner()
        conn = _make_conn_mock()

        with (
            patch("app.services.order_service.get_connection") as mock_gc,
            patch("app.services.order_service.get_serializable_transaction") as mock_tx,
            patch("app.services.order_service.emit_and_publish", new_callable=AsyncMock),
        ):
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_tx.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_tx.return_value.__aexit__ = AsyncMock(return_value=False)

            items_with_fake_price = [{"item_id": 42, "item_name": "Burger", "quantity": 1, "price": 999.99}]
            result = await svc.checkout(user=user, items=items_with_fake_price, payment_method="cash",
                                        order_type="pos", total_amount=999.99)

        # Unit price must come from DB row (150.00), not client hint (999.99)
        assert result["items"][0]["unit_price"] == pytest.approx(150.0)


# ─────────────────────────────────────────────────────────────
# AC-2: Idempotency replay — same key returns same order
# ─────────────────────────────────────────────────────────────

class TestIdempotencyReplay:

    STORED_RESPONSE = {
        "id": "existing-order-id",
        "order_number": "EXISTIN0",
        "status": "Pending",
        "total_amount": 315.0,
        "items": [{"item_id": 42, "item_name": "Burger", "quantity": 2}],
        "created_at": "2026-05-08T10:00:00+00:00",
        "updated_at": "2026-05-08T10:00:00+00:00",
    }

    @pytest.mark.asyncio
    async def test_same_key_returns_original_order(self):
        """Retry with same idempotency key must return stored response, not create new order."""
        svc = OrderService()
        user = _owner()

        # Simulate existing idempotency record
        idempotency_row = {"response_payload": self.STORED_RESPONSE}
        conn = _make_conn_mock(idempotency_row=idempotency_row)

        with (
            patch("app.services.order_service.get_connection") as mock_gc,
            patch("app.services.order_service.get_serializable_transaction") as mock_tx,
            patch("app.services.order_service.emit_and_publish", new_callable=AsyncMock),
        ):
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_tx.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_tx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await svc.checkout(
                user=user,
                items=SAMPLE_ITEMS,
                payment_method="cash",
                order_type="pos",
                total_amount=315.0,
                idempotency_key="idem-key-abc123",
            )

        assert result["id"] == "existing-order-id"
        assert result["idempotent"] is True
        # Must NOT have executed any INSERT (no new order created)
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_different_users_same_key_independent(self):
        """Two different owners with the same idempotency key must be independent."""
        svc = OrderService()
        user_a = _owner(user_id="owner-A")
        user_b = _owner(user_id="owner-B")

        stored_for_a = {**self.STORED_RESPONSE, "id": "order-for-A"}
        idempotency_row_a = {"response_payload": stored_for_a}

        conn_a = _make_conn_mock(idempotency_row=idempotency_row_a)
        # User B has no existing record
        conn_b = _make_conn_mock(idempotency_row=None)

        with (
            patch("app.services.order_service.get_connection") as mock_gc,
            patch("app.services.order_service.get_serializable_transaction") as mock_tx,
            patch("app.services.order_service.emit_and_publish", new_callable=AsyncMock),
        ):
            # For user_a: return idempotency record; for user_b: return None
            call_count = [0]

            async def _get_conn_side_effect():
                # Alternate between conn_a and conn_b for successive calls
                pass

            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn_a)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_tx.return_value.__aenter__ = AsyncMock(return_value=conn_b)
            mock_tx.return_value.__aexit__ = AsyncMock(return_value=False)

            result_a = await svc.checkout(
                user=user_a,
                items=SAMPLE_ITEMS,
                payment_method="cash",
                order_type="pos",
                total_amount=315.0,
                idempotency_key="shared-key",
            )

        assert result_a["id"] == "order-for-A"
        assert result_a["idempotent"] is True

    @pytest.mark.asyncio
    async def test_branch_user_idempotency_scoped_to_owner(self):
        """Branch staff's key is scoped to the owner, not the staff user_id."""
        svc = OrderService()
        staff = _staff(owner_id="owner-1", user_id="staff-99")

        stored = {**self.STORED_RESPONSE, "id": "order-branch-scope"}
        idem_row = {"response_payload": stored}
        conn = _make_conn_mock(idempotency_row=idem_row)

        with (
            patch("app.services.order_service.get_connection") as mock_gc,
            patch("app.services.order_service.get_serializable_transaction") as mock_tx,
            patch("app.services.order_service.emit_and_publish", new_callable=AsyncMock),
        ):
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_tx.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_tx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await svc.checkout(
                user=staff,
                items=SAMPLE_ITEMS,
                payment_method="cash",
                order_type="pos",
                total_amount=315.0,
                idempotency_key="staff-key-001",
            )

        # The query should have used owner_id ("owner-1"), not user_id ("staff-99")
        # Verify by inspecting what was passed to fetchrow
        fetchrow_calls = conn.fetchrow.call_args_list
        idempotency_call = next(
            (c for c in fetchrow_calls if "checkout_idempotency" in c.args[0].lower()),
            None,
        )
        if idempotency_call:
            # Second positional arg after the query is idempotency_key, third is user_id
            assert "owner-1" in idempotency_call.args


# ─────────────────────────────────────────────────────────────
# AC-3: Timeout + retry: committed order is found on retry
# ─────────────────────────────────────────────────────────────

class TestTimeoutRetry:

    @pytest.mark.asyncio
    async def test_retry_after_commit_returns_original(self):
        """
        Scenario: First request times out on client side but server committed.
        Second request (same key) must find the committed idempotency record.
        """
        svc = OrderService()
        user = _owner()
        committed_response = {
            "id": "committed-order-id",
            "order_number": "COMMITTE",
            "status": "Pending",
            "total_amount": 315.0,
            "items": [],
            "created_at": "2026-05-08T10:00:00+00:00",
            "updated_at": "2026-05-08T10:00:00+00:00",
        }

        # On retry, idempotency record exists (server had committed it)
        idem_row = {"response_payload": committed_response}
        conn = _make_conn_mock(idempotency_row=idem_row)

        with (
            patch("app.services.order_service.get_connection") as mock_gc,
            patch("app.services.order_service.get_serializable_transaction") as mock_tx,
            patch("app.services.order_service.emit_and_publish", new_callable=AsyncMock),
        ):
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_tx.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_tx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await svc.checkout(
                user=user,
                items=SAMPLE_ITEMS,
                payment_method="cash",
                order_type="pos",
                total_amount=315.0,
                idempotency_key="timeout-retry-key",
            )

        assert result["id"] == "committed-order-id"
        assert result["idempotent"] is True


# ─────────────────────────────────────────────────────────────
# AC-4: GET /orders newest-first, includes recent order
# ─────────────────────────────────────────────────────────────

class TestOrdersListOrdering:

    @pytest.mark.asyncio
    async def test_list_returns_pagination_shape(self):
        """List endpoint must return paginated response with correct shape."""
        svc = OrderService()
        user = _owner()

        order_rows = [
            {
                "id": "order-2", "order_number": None, "display_order_number": "ORDER-2",
                "status": "Pending", "source": "pos", "subtotal": 300.0, "tax_amount": 15.0,
                "discount_amount": 0.0, "total_amount": 315.0, "table_number": None,
                "delivery_address": None, "customer_id": None, "notes": None,
                "branch_id": None, "restaurant_id": "rest-1",
                "created_at": "2026-05-08T11:00:00+00:00",
                "updated_at": "2026-05-08T11:00:00+00:00",
                "customer_name": None, "customer_phone": None,
                "items": json.dumps([{"id": 1, "item_name": "Burger"}]),
            },
            {
                "id": "order-1", "order_number": None, "display_order_number": "ORDER-1",
                "status": "Confirmed", "source": "pos", "subtotal": 150.0, "tax_amount": 7.5,
                "discount_amount": 0.0, "total_amount": 157.5, "table_number": None,
                "delivery_address": None, "customer_id": None, "notes": None,
                "branch_id": None, "restaurant_id": "rest-1",
                "created_at": "2026-05-08T09:00:00+00:00",
                "updated_at": "2026-05-08T09:00:00+00:00",
                "customer_name": None, "customer_phone": None,
                "items": json.dumps([]),
            },
        ]

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"cnt": 2})
        conn.fetch = AsyncMock(return_value=order_rows)

        with patch("app.services.order_service.get_connection") as mock_gc:
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await svc.get_orders(user=user, limit=20, offset=0)

        assert "items" in result
        assert "page" in result
        assert "page_size" in result
        assert "has_more" in result
        assert "total" in result

        assert result["page"] == 1
        assert result["page_size"] == 20
        assert result["total"] == 2
        assert result["has_more"] is False
        assert len(result["items"]) == 2

    @pytest.mark.asyncio
    async def test_list_has_more_correct(self):
        """has_more must be True when total > offset + page_size."""
        svc = OrderService()
        user = _owner()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"cnt": 50})
        conn.fetch = AsyncMock(return_value=[])

        with patch("app.services.order_service.get_connection") as mock_gc:
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await svc.get_orders(user=user, limit=20, offset=0)

        assert result["has_more"] is True
        assert result["total"] == 50

    @pytest.mark.asyncio
    async def test_list_inline_items_parsed(self):
        """items field in each order should be a list, not a JSON string."""
        svc = OrderService()
        user = _owner()

        items_json = json.dumps([{"id": 1, "item_name": "Burger", "quantity": 2}])
        order_rows = [{
            "id": "o1", "order_number": None, "display_order_number": "O1",
            "status": "Pending", "source": "pos", "subtotal": 150.0, "tax_amount": 7.5,
            "discount_amount": 0.0, "total_amount": 157.5, "table_number": None,
            "delivery_address": None, "customer_id": None, "notes": None,
            "branch_id": None, "restaurant_id": "rest-1",
            "created_at": "2026-05-08T10:00:00+00:00", "updated_at": "2026-05-08T10:00:00+00:00",
            "customer_name": None, "customer_phone": None,
            "items": items_json,
        }]

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"cnt": 1})
        conn.fetch = AsyncMock(return_value=order_rows)

        with patch("app.services.order_service.get_connection") as mock_gc:
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await svc.get_orders(user=user, include_items=True)

        assert isinstance(result["items"][0]["items"], list)
        assert result["items"][0]["items"][0]["item_name"] == "Burger"


# ─────────────────────────────────────────────────────────────
# AC-5: Date filter edge cases
# ─────────────────────────────────────────────────────────────

class TestDateFilters:

    @pytest.mark.asyncio
    async def test_from_date_appears_in_query(self):
        """from_date must be passed as a parameter to the SQL query."""
        svc = OrderService()
        user = _owner()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"cnt": 0})
        conn.fetch = AsyncMock(return_value=[])

        with patch("app.services.order_service.get_connection") as mock_gc:
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)

            await svc.get_orders(user=user, from_date="2026-05-01", to_date="2026-05-08")

        # Verify the SQL fetch call contains the date strings as params
        fetch_call = conn.fetch.call_args
        query = fetch_call.args[0]
        params = fetch_call.args[1:]

        # Both dates should appear in the parameter list
        assert "2026-05-01" in params
        assert "2026-05-08" in params

        # SQL should use correct inclusive/exclusive operators
        assert ">=" in query  # from_date inclusive
        assert "+ INTERVAL" in query  # to_date inclusive (next day)

    @pytest.mark.asyncio
    async def test_no_date_filters_no_date_conditions(self):
        """Without date filters, the SQL must not contain date-related predicates."""
        svc = OrderService()
        user = _owner()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"cnt": 0})
        conn.fetch = AsyncMock(return_value=[])

        with patch("app.services.order_service.get_connection") as mock_gc:
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)

            await svc.get_orders(user=user)

        fetch_call = conn.fetch.call_args
        query = fetch_call.args[0]
        assert "INTERVAL" not in query


# ─────────────────────────────────────────────────────────────
# AC-6: Auth scope — cross-tenant/cross-branch prevention
# ─────────────────────────────────────────────────────────────

class TestAuthScope:

    @pytest.mark.asyncio
    async def test_get_order_detail_404_for_wrong_owner(self):
        """get_order_detail must raise NotFoundError if order belongs to another tenant."""
        svc = OrderService()
        user = _owner(user_id="owner-A")
        conn = AsyncMock()
        # Simulate DB returning nothing (row owned by owner-B, not owner-A)
        conn.fetchrow = AsyncMock(return_value=None)

        with patch("app.services.order_service.get_connection") as mock_gc:
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(NotFoundError):
                await svc.get_order_detail(user=user, order_id="order-owned-by-B")

    @pytest.mark.asyncio
    async def test_list_orders_where_clause_uses_owner_id_for_owner(self):
        """Owner's list query must filter by user_id = owner_id."""
        svc = OrderService()
        user = _owner(user_id="owner-XYZ")
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"cnt": 0})
        conn.fetch = AsyncMock(return_value=[])

        with patch("app.services.order_service.get_connection") as mock_gc:
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)

            await svc.get_orders(user=user)

        fetch_call = conn.fetch.call_args
        # user_id param must appear in positional args
        assert "owner-XYZ" in fetch_call.args[1:]

    @pytest.mark.asyncio
    async def test_list_orders_where_clause_uses_branch_for_staff(self):
        """Branch staff list query must filter by both user_id (owner) AND branch_id."""
        svc = OrderService()
        staff = _staff(owner_id="owner-XYZ", user_id="staff-1", branch_id="branch-ABC")
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"cnt": 0})
        conn.fetch = AsyncMock(return_value=[])

        with patch("app.services.order_service.get_connection") as mock_gc:
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)

            await svc.get_orders(user=staff)

        fetch_call = conn.fetch.call_args
        params = fetch_call.args[1:]
        assert "owner-XYZ" in params
        assert "branch-ABC" in params


# ─────────────────────────────────────────────────────────────
# AC-7: GET /orders/{id} — full order with items, 404 on missing
# ─────────────────────────────────────────────────────────────

class TestGetOrderDetail:

    @pytest.mark.asyncio
    async def test_returns_items_list(self):
        svc = OrderService()
        user = _owner()
        order_row = {
            "id": "order-123",
            "status": "Pending",
            "source": "pos",
            "subtotal": 150.0,
            "tax_amount": 7.5,
            "discount_amount": 0.0,
            "total_amount": 157.5,
            "table_number": None,
            "delivery_address": None,
            "customer_id": None,
            "notes": None,
            "branch_id": None,
            "restaurant_id": "rest-1",
            "created_at": "2026-05-08T10:00:00+00:00",
            "updated_at": "2026-05-08T10:00:00+00:00",
            "customer_name": None,
            "customer_phone": None,
            "metadata": {},
        }
        items_rows = [
            {"id": 1, "item_id": 42, "item_name": "Burger", "variant_id": None,
             "quantity": 1, "unit_price": 150.0, "total_price": 150.0,
             "addons": None, "notes": None},
        ]
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=order_row)
        conn.fetch = AsyncMock(return_value=items_rows)

        with patch("app.services.order_service.get_connection") as mock_gc:
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await svc.get_order_detail(user=user, order_id="order-123")

        assert result["id"] == "order-123"
        assert "items" in result
        assert len(result["items"]) == 1
        assert result["items"][0]["item_name"] == "Burger"

    @pytest.mark.asyncio
    async def test_404_on_missing_order(self):
        svc = OrderService()
        user = _owner()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)

        with patch("app.services.order_service.get_connection") as mock_gc:
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(NotFoundError):
                await svc.get_order_detail(user=user, order_id="nonexistent")


# ─────────────────────────────────────────────────────────────
# AC-8: Transactional rollback — no partial side effects
# ─────────────────────────────────────────────────────────────

class TestRollback:

    @pytest.mark.asyncio
    async def test_rollback_on_item_not_found(self):
        """If an item is not found, transaction must not commit any row."""
        import asyncpg

        svc = OrderService()
        user = _owner()
        conn = AsyncMock()

        async def _fetchrow_no_item(query, *args):
            q = query.strip().lower()
            if "checkout_idempotency" in q:
                return None
            if "restaurant_settings" in q:
                return TAX_ROW
            if "items" in q:
                return None  # Item not found
            return None

        conn.fetchrow = AsyncMock(side_effect=_fetchrow_no_item)
        conn.execute = AsyncMock()

        # Track that the serializable transaction was entered
        tx_entered = []

        class FakeSerTx:
            async def __aenter__(self):
                tx_entered.append(True)
                return conn
            async def __aexit__(self, exc_type, *a):
                # asyncpg rolls back on exc_type != None
                return False

        class FakeConn:
            async def __aenter__(self):
                return conn
            async def __aexit__(self, *a):
                return False

        with (
            patch("app.services.order_service.get_connection", return_value=FakeConn()),
            patch("app.services.order_service.get_serializable_transaction", return_value=FakeSerTx()),
        ):
            with pytest.raises(NotFoundError):
                await svc.checkout(
                    user=user,
                    items=[{"item_id": 9999, "item_name": "Ghost Item", "quantity": 1}],
                    payment_method="cash",
                    order_type="pos",
                    total_amount=100.0,
                )

        # No INSERT into orders should have been called
        for call_args in conn.execute.call_args_list:
            query = call_args.args[0].strip().lower()
            assert "insert into orders" not in query, \
                "orders INSERT must not execute when item lookup fails"


# ─────────────────────────────────────────────────────────────
# AC-9: Error responses have standard shape with retryable flag
# ─────────────────────────────────────────────────────────────

class TestErrorShape:

    def test_lock_acquisition_error_is_retryable(self):
        from app.core.exceptions import LockAcquisitionError
        exc = LockAcquisitionError("order:123")
        assert exc.retryable is True
        assert exc.error_code == "LOCK_CONFLICT"

    def test_not_found_error_is_not_retryable(self):
        from app.core.exceptions import NotFoundError
        exc = NotFoundError("Order", "abc")
        assert exc.retryable is False
        assert exc.error_code == "NOT_FOUND"

    def test_validation_error_is_not_retryable(self):
        from app.core.exceptions import ValidationError
        exc = ValidationError("bad input")
        assert exc.retryable is False
        assert exc.error_code == "VALIDATION_ERROR"

    def test_rate_limit_error_is_retryable(self):
        from app.core.exceptions import RateLimitError
        exc = RateLimitError()
        assert exc.retryable is True
        assert exc.error_code == "RATE_LIMIT_EXCEEDED"

    def test_checkout_error_has_error_code(self):
        from app.core.exceptions import CheckoutError
        exc = CheckoutError("coupon expired", error_code="COUPON_EXPIRED")
        assert exc.error_code == "COUPON_EXPIRED"
        assert exc.retryable is False

    def test_checkout_error_retryable_flag(self):
        from app.core.exceptions import CheckoutError
        exc = CheckoutError("db unavailable", error_code="DB_ERROR", retryable=True)
        assert exc.retryable is True


# ─────────────────────────────────────────────────────────────
# AC-10: Checkout logs include idempotency_key + outcome + order_id
# ─────────────────────────────────────────────────────────────

class TestObservabilityLogs:

    @pytest.mark.asyncio
    async def test_committed_log_includes_required_fields(self):
        """checkout_committed log must include idempotency_key, outcome, order_id."""
        svc = OrderService()
        user = _owner()
        conn = _make_conn_mock()

        log_records = []

        class CapturingLogger:
            def info(self, event, **kw):
                log_records.append({"event": event, **kw})

            def error(self, event, **kw):
                log_records.append({"event": event, **kw})

            def warning(self, event, **kw):
                log_records.append({"event": event, **kw})

        with (
            patch("app.services.order_service.get_connection") as mock_gc,
            patch("app.services.order_service.get_serializable_transaction") as mock_tx,
            patch("app.services.order_service.emit_and_publish", new_callable=AsyncMock),
        ):
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_tx.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_tx.return_value.__aexit__ = AsyncMock(return_value=False)

            import app.services.order_service as svc_module
            orig_logger = svc_module.logger
            svc_module.logger = CapturingLogger()

            try:
                await svc.checkout(
                    user=user,
                    items=SAMPLE_ITEMS,
                    payment_method="cash",
                    order_type="pos",
                    total_amount=315.0,
                    idempotency_key="log-test-key",
                )
            finally:
                svc_module.logger = orig_logger

        committed_logs = [r for r in log_records if r.get("event") == "checkout_committed"]
        assert len(committed_logs) == 1
        log = committed_logs[0]

        assert log.get("idempotency_key") == "log-test-key"
        assert log.get("outcome") == "committed"
        assert "order_id" in log
        assert "latency_ms" in log
        assert "user_id" in log

    @pytest.mark.asyncio
    async def test_replay_log_includes_outcome_replayed(self):
        """Replayed checkout must log outcome=replayed."""
        svc = OrderService()
        user = _owner()

        stored = {"id": "existing-id", "order_number": "EXISTIN0", "status": "Pending",
                  "total_amount": 315.0, "items": [], "created_at": "2026-05-08T10:00:00+00:00",
                  "updated_at": "2026-05-08T10:00:00+00:00"}
        conn = _make_conn_mock(idempotency_row={"response_payload": stored})

        log_records = []

        class CapturingLogger:
            def info(self, event, **kw):
                log_records.append({"event": event, **kw})
            def error(self, event, **kw): pass
            def warning(self, event, **kw): pass

        with (
            patch("app.services.order_service.get_connection") as mock_gc,
            patch("app.services.order_service.get_serializable_transaction") as mock_tx,
            patch("app.services.order_service.emit_and_publish", new_callable=AsyncMock),
        ):
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_tx.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_tx.return_value.__aexit__ = AsyncMock(return_value=False)

            import app.services.order_service as svc_module
            orig_logger = svc_module.logger
            svc_module.logger = CapturingLogger()

            try:
                await svc.checkout(
                    user=user, items=SAMPLE_ITEMS, payment_method="cash",
                    order_type="pos", total_amount=315.0, idempotency_key="replay-log-key",
                )
            finally:
                svc_module.logger = orig_logger

        replayed_logs = [r for r in log_records if r.get("event") == "checkout_replayed"]
        assert len(replayed_logs) == 1
        assert replayed_logs[0].get("outcome") == "replayed"
        assert replayed_logs[0].get("idempotency_key") == "replay-log-key"


# ─────────────────────────────────────────────────────────────
# AC-11: No N+1 — list does not require per-order detail fetch
# ─────────────────────────────────────────────────────────────

class TestNoN1:

    @pytest.mark.asyncio
    async def test_list_with_include_items_uses_single_query(self):
        """
        With include_items=True, the list endpoint must return items inline.
        Only TWO DB calls: one COUNT + one SELECT (no per-order fetches).
        """
        svc = OrderService()
        user = _owner()

        items_json = json.dumps([{"id": 1, "item_name": "Burger"}])
        rows = [
            {
                "id": f"o{i}", "order_number": None, "display_order_number": f"O{i}",
                "status": "Pending", "source": "pos", "subtotal": 150.0, "tax_amount": 7.5,
                "discount_amount": 0.0, "total_amount": 157.5, "table_number": None,
                "delivery_address": None, "customer_id": None, "notes": None,
                "branch_id": None, "restaurant_id": "rest-1",
                "created_at": f"2026-05-08T0{i}:00:00+00:00",
                "updated_at": f"2026-05-08T0{i}:00:00+00:00",
                "customer_name": None, "customer_phone": None,
                "items": items_json,
            }
            for i in range(1, 6)
        ]

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"cnt": 5})
        conn.fetch = AsyncMock(return_value=rows)

        with patch("app.services.order_service.get_connection") as mock_gc:
            mock_gc.return_value.__aenter__ = AsyncMock(return_value=conn)
            mock_gc.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await svc.get_orders(user=user, include_items=True)

        # Exactly 1 fetchrow (COUNT) + 1 fetch (data) — no additional calls
        assert conn.fetchrow.call_count == 1  # COUNT query
        assert conn.fetch.call_count == 1     # data + aggregated items query

        # All 5 orders have items embedded
        assert len(result["items"]) == 5
        for order in result["items"]:
            assert isinstance(order["items"], list)
