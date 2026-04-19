"""
RBAC Acceptance Tests
=====================
Validates the four key acceptance criteria:
  1. Can a waiter issue a refund?           → NO (403)
  2. Can a cashier give a 50 % discount?    → NO (max_discount_percent=10)
  3. Are activity logs generated?            → YES
  4. Can a branch user access another branch's data? → NO
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.core.auth import UserContext
from app.services.rbac_service import RBACService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def rbac():
    return RBACService()


@pytest.fixture
def waiter_ctx():
    return UserContext(
        user_id="waiter-001",
        email="waiter@test.com",
        role="waiter",
        restaurant_id="rest-001",
        branch_id="branch-A",
        owner_id="owner-001",
        is_branch_user=True,
    )


@pytest.fixture
def cashier_ctx():
    return UserContext(
        user_id="cashier-001",
        email="cashier@test.com",
        role="cashier",
        restaurant_id="rest-001",
        branch_id="branch-A",
        owner_id="owner-001",
        is_branch_user=True,
    )


@pytest.fixture
def manager_ctx():
    return UserContext(
        user_id="manager-001",
        email="manager@test.com",
        role="manager",
        restaurant_id="rest-001",
        branch_id="branch-A",
        owner_id="owner-001",
        is_branch_user=True,
    )


@pytest.fixture
def branch_b_cashier_ctx():
    return UserContext(
        user_id="cashier-002",
        email="cashier2@test.com",
        role="cashier",
        restaurant_id="rest-001",
        branch_id="branch-B",
        owner_id="owner-001",
        is_branch_user=True,
    )


# ---------------------------------------------------------------------------
# Helper: force fallback path (DB unavailable)
# ---------------------------------------------------------------------------

def _patch_db_unavailable():
    """Patches get_connection so RBAC falls back to hardcoded permissions."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=[])
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    return patch("app.services.rbac_service.get_connection", return_value=cm)


def _patch_caches():
    """Patches redis + local cache so every call goes to fallback."""
    return [
        patch("app.services.rbac_service.cache_get", AsyncMock(return_value=None)),
        patch("app.services.rbac_service.cache_set", AsyncMock()),
        patch("app.services.rbac_service._LOCAL_CACHE", {}),
    ]


# ===================================================================
# ACCEPTANCE TEST 1 — Waiter cannot refund
# ===================================================================

class TestWaiterCannotRefund:
    @pytest.mark.asyncio
    async def test_waiter_has_no_refund_permission(self, rbac, waiter_ctx):
        """Waiter must NOT hold payment.refund."""
        with _patch_db_unavailable():
            for p in _patch_caches():
                p.start()
            try:
                decision = await rbac.check_permission(waiter_ctx, "payment.refund")
                assert decision.allowed is False, "Waiter should NOT be allowed to refund"
            finally:
                for p in _patch_caches():
                    p.stop()

    @pytest.mark.asyncio
    async def test_waiter_has_no_payment_create(self, rbac, waiter_ctx):
        """Waiter must NOT hold payment.create either."""
        with _patch_db_unavailable():
            for p in _patch_caches():
                p.start()
            try:
                decision = await rbac.check_permission(waiter_ctx, "payment.create")
                assert decision.allowed is False, "Waiter should NOT create payments"
            finally:
                for p in _patch_caches():
                    p.stop()

    @pytest.mark.asyncio
    async def test_waiter_can_create_orders(self, rbac, waiter_ctx):
        """Sanity check — waiter CAN create orders."""
        with _patch_db_unavailable():
            for p in _patch_caches():
                p.start()
            try:
                decision = await rbac.check_permission(waiter_ctx, "order.create")
                assert decision.allowed is True, "Waiter should be able to create orders"
            finally:
                for p in _patch_caches():
                    p.stop()


# ===================================================================
# ACCEPTANCE TEST 2 — Cashier cannot give 50 % discount
# ===================================================================

class TestCashierDiscountLimit:
    @pytest.mark.asyncio
    async def test_cashier_discount_meta_has_limit(self, rbac, cashier_ctx):
        """Cashier billing.discount must carry max_discount_percent=10."""
        with _patch_db_unavailable():
            for p in _patch_caches():
                p.start()
            try:
                decision = await rbac.check_permission(cashier_ctx, "billing.discount")
                assert decision.allowed is True, "Cashier should be able to discount"
                assert decision.meta.get("max_discount_percent") == 10, (
                    f"Expected max 10%, got {decision.meta}"
                )
            finally:
                for p in _patch_caches():
                    p.stop()

    @pytest.mark.asyncio
    async def test_manager_discount_meta_has_higher_limit(self, rbac, manager_ctx):
        """Manager billing.discount must carry max_discount_percent=50."""
        with _patch_db_unavailable():
            for p in _patch_caches():
                p.start()
            try:
                decision = await rbac.check_permission(manager_ctx, "billing.discount")
                assert decision.allowed is True
                assert decision.meta.get("max_discount_percent") == 50
            finally:
                for p in _patch_caches():
                    p.stop()

    @pytest.mark.asyncio
    async def test_manager_refund_meta_has_limit(self, rbac, manager_ctx):
        """Manager payment.refund must carry max_refund_amount=5000."""
        with _patch_db_unavailable():
            for p in _patch_caches():
                p.start()
            try:
                decision = await rbac.check_permission(manager_ctx, "payment.refund")
                assert decision.allowed is True
                assert decision.meta.get("max_refund_amount") == 5000
            finally:
                for p in _patch_caches():
                    p.stop()

    @pytest.mark.asyncio
    async def test_cashier_has_no_refund(self, rbac, cashier_ctx):
        """Cashier cannot refund at all."""
        with _patch_db_unavailable():
            for p in _patch_caches():
                p.start()
            try:
                decision = await rbac.check_permission(cashier_ctx, "payment.refund")
                assert decision.allowed is False
            finally:
                for p in _patch_caches():
                    p.stop()


# ===================================================================
# ACCEPTANCE TEST 3 — Activity logs generated
# ===================================================================

class TestActivityLogging:
    @pytest.mark.asyncio
    async def test_log_activity_inserts_row(self):
        """log_activity must INSERT into activity_logs."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mock_conn)
        cm.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.activity_log_service.get_connection", return_value=cm):
            from app.services.activity_log_service import log_activity
            await log_activity(
                user_id="user-1",
                action="order.created",
                entity_type="order",
                entity_id="order-123",
                metadata={"source": "pos"},
                branch_id="branch-A",
            )

            mock_conn.execute.assert_called_once()
            call_args = mock_conn.execute.call_args
            sql = call_args[0][0]
            assert "INSERT INTO activity_logs" in sql
            assert call_args[0][1] == "user-1"
            assert call_args[0][2] == "branch-A"
            assert call_args[0][3] == "order.created"

    @pytest.mark.asyncio
    async def test_log_activity_does_not_raise_on_failure(self):
        """Activity logging failure must NOT propagate to the caller."""
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(side_effect=Exception("db down"))
        cm.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.activity_log_service.get_connection", return_value=cm):
            from app.services.activity_log_service import log_activity
            # Must not raise
            await log_activity(
                user_id="u", action="test", entity_type="t",
            )


# ===================================================================
# ACCEPTANCE TEST 4 — Cross-branch isolation
# ===================================================================

class TestBranchIsolation:
    @pytest.mark.asyncio
    async def test_rbac_branch_mismatch_returns_empty(self, rbac):
        """If branch_user's branch doesn't match role branch → no permissions."""
        # Simulate a branch user whose DB row says branch_id=branch-X
        # but their JWT says branch_id=branch-A  →  mismatch  →  empty map
        user = UserContext(
            user_id="user-mismatch",
            email="m@test.com",
            role="cashier",
            branch_id="branch-A",
            owner_id="owner-001",
            is_branch_user=True,
        )

        mock_row = {
            "role_id": "role-1",
            "branch_role": "cashier",
            "branch_id": "branch-X",     # DIFFERENT from user's branch-A
            "role_name": "cashier",
            "role_branch_id": "branch-X",
        }
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=mock_row)
        mock_conn.fetch = AsyncMock(return_value=[])
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mock_conn)
        cm.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.rbac_service.get_connection", return_value=cm):
            for p in _patch_caches():
                p.start()
            try:
                decision = await rbac.check_permission(user, "order.read")
                assert decision.allowed is False, (
                    "Branch mismatch must deny all permissions"
                )
            finally:
                for p in _patch_caches():
                    p.stop()

    def test_tenant_where_clause_includes_branch_for_branch_users(self):
        """tenant_where_clause must filter by branch_id for branch users."""
        from app.core.tenant import tenant_where_clause

        branch_user = UserContext(
            user_id="bu-1",
            role="cashier",
            branch_id="branch-A",
            owner_id="owner-001",
            is_branch_user=True,
        )
        clause, params = tenant_where_clause(branch_user, "o")
        assert "branch_id" in clause, "Branch users must be filtered by branch_id"
        assert params == ["owner-001", "branch-A"]

    def test_tenant_where_clause_no_branch_for_owners(self):
        """Owners see all branches — no branch_id filter."""
        from app.core.tenant import tenant_where_clause

        owner = UserContext(
            user_id="owner-001",
            role="owner",
            is_branch_user=False,
        )
        clause, params = tenant_where_clause(owner, "o")
        assert "branch_id" not in clause
        assert params == ["owner-001"]
