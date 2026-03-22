"""
Unit tests for tenant isolation.
"""
import pytest
from app.core.auth import UserContext
from app.core.tenant import (
    tenant_filter,
    tenant_where_clause,
    tenant_insert_fields,
    build_tenant_query,
)


class TestTenantFilter:
    def test_owner_filter(self, owner_context):
        result = tenant_filter(owner_context)
        assert result == {"user_id": owner_context.user_id}

    def test_branch_user_filter(self, manager_context):
        result = tenant_filter(manager_context)
        assert result == {
            "user_id": manager_context.owner_id,
            "branch_id": manager_context.branch_id,
        }


class TestTenantWhereClause:
    def test_owner_clause(self, owner_context):
        clause, params = tenant_where_clause(owner_context)
        assert "user_id = $1" in clause
        assert params == [owner_context.user_id]

    def test_branch_user_clause(self, manager_context):
        clause, params = tenant_where_clause(manager_context)
        assert "user_id = $1" in clause
        assert "branch_id = $2" in clause
        assert params == [manager_context.owner_id, manager_context.branch_id]

    def test_with_alias(self, owner_context):
        clause, params = tenant_where_clause(owner_context, "o")
        assert "o.user_id = $1" in clause


class TestTenantInsertFields:
    def test_owner_insert(self, owner_context):
        fields = tenant_insert_fields(owner_context)
        assert fields["user_id"] == owner_context.user_id
        assert "branch_id" in fields

    def test_branch_user_insert(self, manager_context):
        fields = tenant_insert_fields(manager_context)
        assert fields["user_id"] == manager_context.owner_id
        assert fields["branch_id"] == manager_context.branch_id


class TestBuildTenantQuery:
    def test_basic_query(self, owner_context):
        query, params = build_tenant_query(
            "SELECT * FROM orders o",
            owner_context,
            table_alias="o",
        )
        assert "WHERE o.user_id = $1" in query
        assert len(params) == 1

    def test_with_extra_conditions(self, owner_context):
        query, params = build_tenant_query(
            "SELECT * FROM orders o",
            owner_context,
            table_alias="o",
            extra_conditions="AND o.status = ${}",
            extra_params=["Pending"],
        )
        assert "AND o.status = $2" in query
        assert params == [owner_context.user_id, "Pending"]
