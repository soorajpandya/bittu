"""
Unit tests for authentication and RBAC.
"""
import pytest
from app.core.auth import (
    UserContext,
    ROLE_HIERARCHY,
    ROLE_PERMISSIONS,
)


class TestRoleHierarchy:
    def test_owner_is_highest(self):
        assert ROLE_HIERARCHY["owner"] > ROLE_HIERARCHY["manager"]

    def test_manager_above_cashier(self):
        assert ROLE_HIERARCHY["manager"] > ROLE_HIERARCHY["cashier"]

    def test_all_roles_defined(self):
        expected = {"owner", "manager", "cashier", "chef", "waiter", "staff"}
        assert set(ROLE_HIERARCHY.keys()) == expected

    def test_hierarchy_values_unique(self):
        values = list(ROLE_HIERARCHY.values())
        assert len(values) == len(set(values))


class TestRolePermissions:
    def test_owner_has_all_wildcard_permissions(self):
        owner_perms = ROLE_PERMISSIONS["owner"]
        assert "orders:*" in owner_perms
        assert "payments:*" in owner_perms
        assert "staff:*" in owner_perms

    def test_cashier_cannot_manage_staff(self):
        cashier_perms = ROLE_PERMISSIONS["cashier"]
        assert "staff:*" not in cashier_perms
        assert "staff:read" not in cashier_perms

    def test_chef_has_kitchen_access(self):
        chef_perms = ROLE_PERMISSIONS["chef"]
        assert "kitchen:*" in chef_perms

    def test_waiter_can_write_orders(self):
        waiter_perms = ROLE_PERMISSIONS["waiter"]
        assert "orders:write" in waiter_perms

    def test_all_roles_have_permissions(self):
        for role in ROLE_HIERARCHY:
            assert role in ROLE_PERMISSIONS


class TestUserContext:
    def test_owner_context(self):
        ctx = UserContext(
            user_id="u1",
            email="a@b.com",
            role="owner",
            restaurant_id="r1",
            branch_id="b1",
        )
        assert ctx.role == "owner"
        assert not ctx.is_branch_user

    def test_branch_user_context(self):
        ctx = UserContext(
            user_id="u2",
            role="manager",
            branch_id="b1",
            owner_id="u1",
            is_branch_user=True,
        )
        assert ctx.is_branch_user
        assert ctx.owner_id == "u1"
