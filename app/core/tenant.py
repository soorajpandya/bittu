"""
Multi-tenant query isolation.
Every DB query MUST be scoped to the user's tenant (owner_id/branch_id).
This module provides safe query builders that prevent cross-tenant data leaks.
"""
from typing import Optional
from app.core.auth import UserContext


def tenant_filter(user: UserContext) -> dict:
    """
    Returns the correct filter dict based on user type.
    Owner users: filter by user_id (they own the data).
    Branch users: filter by branch_id (scoped to their branch).
    """
    if user.is_branch_user:
        return {"user_id": user.owner_id, "branch_id": user.branch_id}
    return {"user_id": user.user_id}


def tenant_where_clause(user: UserContext, table_alias: str = "") -> tuple[str, list]:
    """
    Returns a WHERE clause fragment and params for raw SQL queries.
    Ensures all queries are tenant-isolated.

    Usage:
        clause, params = tenant_where_clause(user, "o")
        query = f"SELECT * FROM orders o WHERE {clause}"
        await conn.fetch(query, *params)
    """
    prefix = f"{table_alias}." if table_alias else ""

    if user.is_branch_user:
        return (
            f"{prefix}user_id = $1 AND {prefix}branch_id = $2",
            [user.owner_id, user.branch_id],
        )
    return (
        f"{prefix}user_id = $1",
        [user.user_id],
    )


def tenant_insert_fields(user: UserContext) -> dict:
    """
    Returns fields to include when inserting a new record.
    Ensures new records are always stamped with correct tenant info.
    """
    fields = {"user_id": user.owner_id if user.is_branch_user else user.user_id}
    if user.branch_id:
        fields["branch_id"] = user.branch_id
    return fields


def build_tenant_query(
    base_query: str,
    user: UserContext,
    table_alias: str = "",
    extra_conditions: str = "",
    extra_params: Optional[list] = None,
) -> tuple[str, list]:
    """
    Build a complete tenant-scoped query.

    Example:
        query, params = build_tenant_query(
            "SELECT * FROM orders o",
            user,
            table_alias="o",
            extra_conditions="AND o.status = ${}",
            extra_params=["Pending"]
        )
    """
    clause, params = tenant_where_clause(user, table_alias)

    if extra_conditions and extra_params:
        # Re-number placeholders for extra params
        offset = len(params)
        for i, param in enumerate(extra_params):
            placeholder = f"${offset + i + 1}"
            extra_conditions = extra_conditions.replace("${}", placeholder, 1)
            params.append(param)

    where = f"WHERE {clause}"
    if extra_conditions:
        where += f" {extra_conditions}"

    return f"{base_query} {where}", params
