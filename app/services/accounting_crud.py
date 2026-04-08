"""Generic CRUD helper for accounting modules.

Provides reusable functions for list / get / create / update / delete
that follow the project's existing pattern (asyncpg raw SQL + tenant isolation).
"""
from typing import Any, Optional
from uuid import UUID
from fastapi import HTTPException
from pydantic import BaseModel

from app.core.auth import UserContext
from app.core.database import get_connection


async def acc_list(
    table: str,
    user: UserContext,
    *,
    pk: str = None,
    filters: Optional[dict] = None,
    order_by: str = "created_at DESC",
    page: int = 1,
    per_page: int = 25,
    search_fields: Optional[dict] = None,
) -> dict:
    """List records with pagination + optional filters."""
    params: list[Any] = []
    conditions: list[str] = []

    # Tenant isolation
    if user.is_branch_user:
        params.extend([user.owner_id, user.branch_id])
        conditions.append(f"t.user_id = ${len(params)-1} AND t.branch_id = ${len(params)}")
    else:
        params.append(user.user_id)
        conditions.append(f"t.user_id = ${len(params)}")

    # Extra filters
    if filters:
        for col, val in filters.items():
            if val is not None:
                params.append(val)
                conditions.append(f"t.{col} = ${len(params)}")

    # Search
    if search_fields:
        for col, val in search_fields.items():
            if val is not None:
                params.append(f"%{val}%")
                conditions.append(f"t.{col} ILIKE ${len(params)}")

    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * per_page

    async with get_connection() as conn:
        count = await conn.fetchval(
            f"SELECT COUNT(*) FROM {table} t WHERE {where}", *params
        )
        params.extend([per_page, offset])
        rows = await conn.fetch(
            f"SELECT * FROM {table} t WHERE {where} ORDER BY t.{order_by} LIMIT ${len(params)-1} OFFSET ${len(params)}",
            *params,
        )
        return {
            "items": [dict(r) for r in rows],
            "page": page,
            "per_page": per_page,
            "total": count,
            "total_pages": (count + per_page - 1) // per_page if count else 0,
        }


async def acc_get(
    table: str,
    pk_col: str,
    pk_val: Any,
    user: UserContext,
    label: str = "Record",
) -> dict:
    """Get a single record by primary key."""
    params: list[Any] = []
    if user.is_branch_user:
        params.extend([user.owner_id, user.branch_id])
        clause = f"t.user_id = $1 AND t.branch_id = $2"
    else:
        params.append(user.user_id)
        clause = f"t.user_id = $1"
    params.append(pk_val)

    async with get_connection() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {table} t WHERE {clause} AND t.{pk_col} = ${len(params)}",
            *params,
        )
        if not row:
            raise HTTPException(status_code=404, detail=f"{label} not found")
        return dict(row)


async def acc_create(
    table: str,
    data: dict,
    user: UserContext,
) -> dict:
    """Insert a record, stamping user_id / branch_id."""
    data["user_id"] = user.owner_id if user.is_branch_user else user.user_id
    if user.branch_id:
        data["branch_id"] = user.branch_id

    cols = list(data.keys())
    vals = list(data.values())
    placeholders = ", ".join(f"${i+1}" for i in range(len(vals)))
    col_str = ", ".join(cols)

    async with get_connection() as conn:
        row = await conn.fetchrow(
            f"INSERT INTO {table} ({col_str}) VALUES ({placeholders}) RETURNING *",
            *vals,
        )
        return dict(row)


async def acc_update(
    table: str,
    pk_col: str,
    pk_val: Any,
    data: dict,
    user: UserContext,
    label: str = "Record",
) -> dict:
    """Update a record by primary key."""
    updates = []
    params: list[Any] = []
    for col, val in data.items():
        params.append(val)
        updates.append(f"{col} = ${len(params)}")

    if not updates:
        return await acc_get(table, pk_col, pk_val, user, label)

    set_clause = ", ".join(updates)

    offset = len(params)
    if user.is_branch_user:
        params.extend([user.owner_id, user.branch_id])
        where = f"user_id = ${offset+1} AND branch_id = ${offset+2}"
    else:
        params.append(user.user_id)
        where = f"user_id = ${offset+1}"
    params.append(pk_val)

    async with get_connection() as conn:
        row = await conn.fetchrow(
            f"UPDATE {table} SET {set_clause}, updated_at = now() WHERE {where} AND {pk_col} = ${len(params)} RETURNING *",
            *params,
        )
        if not row:
            raise HTTPException(status_code=404, detail=f"{label} not found")
        return dict(row)


async def acc_delete(
    table: str,
    pk_col: str,
    pk_val: Any,
    user: UserContext,
    label: str = "Record",
) -> dict:
    """Delete a record by primary key."""
    params: list[Any] = []
    if user.is_branch_user:
        params.extend([user.owner_id, user.branch_id])
        clause = f"user_id = $1 AND branch_id = $2"
    else:
        params.append(user.user_id)
        clause = f"user_id = $1"
    params.append(pk_val)

    async with get_connection() as conn:
        result = await conn.execute(
            f"DELETE FROM {table} WHERE {clause} AND {pk_col} = ${len(params)}",
            *params,
        )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail=f"{label} not found")
        return {"message": f"{label} deleted"}


async def acc_status_update(
    table: str,
    pk_col: str,
    pk_val: Any,
    new_status: str,
    user: UserContext,
    label: str = "Record",
) -> dict:
    """Update just the status column."""
    return await acc_update(table, pk_col, pk_val, {"status": new_status}, user, label)


async def acc_line_items_create(
    parent_id: Any,
    parent_type: str,
    line_items: list[dict],
    user: UserContext,
) -> list[dict]:
    """Create line items for a parent document."""
    results = []
    for li in line_items:
        li["parent_id"] = parent_id
        li["parent_type"] = parent_type
        row = await acc_create("acc_line_items", li, user)
        results.append(row)
    return results


async def acc_line_items_replace(
    parent_id: Any,
    parent_type: str,
    line_items: list[dict],
    user: UserContext,
) -> list[dict]:
    """Delete existing line items and insert new ones."""
    params: list[Any] = []
    if user.is_branch_user:
        params.extend([user.owner_id, user.branch_id])
        clause = f"user_id = $1 AND branch_id = $2"
    else:
        params.append(user.user_id)
        clause = f"user_id = $1"
    params.extend([parent_id, parent_type])

    async with get_connection() as conn:
        await conn.execute(
            f"DELETE FROM acc_line_items WHERE {clause} AND parent_id = ${len(params)-1} AND parent_type = ${len(params)}",
            *params,
        )
    return await acc_line_items_create(parent_id, parent_type, line_items, user)


async def acc_line_items_get(
    parent_id: Any,
    parent_type: str,
    user: UserContext,
) -> list[dict]:
    """Get line items for a parent document."""
    params: list[Any] = []
    if user.is_branch_user:
        params.extend([user.owner_id, user.branch_id])
        clause = f"t.user_id = $1 AND t.branch_id = $2"
    else:
        params.append(user.user_id)
        clause = f"t.user_id = $1"
    params.extend([parent_id, parent_type])

    async with get_connection() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM acc_line_items t WHERE {clause} AND t.parent_id = ${len(params)-1} AND t.parent_type = ${len(params)} ORDER BY t.item_order",
            *params,
        )
        return [dict(r) for r in rows]


# ── Comments ────────────────────────────────────────────────────

async def acc_comments_list(
    table: str, pk_col: str, pk_val: Any, user: UserContext,
) -> list[dict]:
    """List comments for a parent record stored as JSONB array."""
    rec = await acc_get(table, pk_col, pk_val, user)
    return rec.get("comments") or []


async def acc_comment_add(
    table: str, pk_col: str, pk_val: Any,
    comment_text: str, user: UserContext, label: str = "Record",
) -> dict:
    import json, uuid, datetime
    rec = await acc_get(table, pk_col, pk_val, user, label)
    comments = rec.get("comments") or []
    entry = {
        "comment_id": str(uuid.uuid4()),
        "description": comment_text,
        "commented_by": str(user.user_id),
        "date": datetime.datetime.utcnow().isoformat(),
    }
    comments.append(entry)
    await acc_update(table, pk_col, pk_val, {"comments": json.dumps(comments)}, user, label)
    return entry


async def acc_comment_update(
    table: str, pk_col: str, pk_val: Any,
    comment_id: str, comment_text: str, user: UserContext, label: str = "Record",
) -> dict:
    import json
    rec = await acc_get(table, pk_col, pk_val, user, label)
    comments = rec.get("comments") or []
    for c in comments:
        if c.get("comment_id") == comment_id:
            c["description"] = comment_text
            await acc_update(table, pk_col, pk_val, {"comments": json.dumps(comments)}, user, label)
            return c
    raise HTTPException(status_code=404, detail="Comment not found")


async def acc_comment_delete(
    table: str, pk_col: str, pk_val: Any,
    comment_id: str, user: UserContext, label: str = "Record",
) -> dict:
    import json
    rec = await acc_get(table, pk_col, pk_val, user, label)
    comments = rec.get("comments") or []
    new = [c for c in comments if c.get("comment_id") != comment_id]
    if len(new) == len(comments):
        raise HTTPException(status_code=404, detail="Comment not found")
    await acc_update(table, pk_col, pk_val, {"comments": json.dumps(new)}, user, label)
    return {"message": "Comment deleted"}


# ── Attachments / Documents ─────────────────────────────────────

async def acc_attachment_get(
    table: str, pk_col: str, pk_val: Any, user: UserContext, label: str = "Record",
) -> list[dict]:
    rec = await acc_get(table, pk_col, pk_val, user, label)
    return rec.get("documents") or []


async def acc_attachment_add(
    table: str, pk_col: str, pk_val: Any,
    attachment: dict, user: UserContext, label: str = "Record",
) -> dict:
    import json, uuid, datetime
    rec = await acc_get(table, pk_col, pk_val, user, label)
    docs = rec.get("documents") or []
    entry = {
        "document_id": str(uuid.uuid4()),
        "file_name": attachment.get("file_name", ""),
        "file_type": attachment.get("file_type", ""),
        "file_size_formatted": attachment.get("file_size_formatted", ""),
        "uploaded_at": datetime.datetime.utcnow().isoformat(),
        **{k: v for k, v in attachment.items() if k not in ("file_name", "file_type", "file_size_formatted")},
    }
    docs.append(entry)
    await acc_update(table, pk_col, pk_val, {"documents": json.dumps(docs)}, user, label)
    return entry


async def acc_attachment_delete(
    table: str, pk_col: str, pk_val: Any,
    document_id: str, user: UserContext, label: str = "Record",
) -> dict:
    import json
    rec = await acc_get(table, pk_col, pk_val, user, label)
    docs = rec.get("documents") or []
    new = [d for d in docs if d.get("document_id") != document_id]
    if len(new) == len(docs):
        raise HTTPException(status_code=404, detail="Document not found")
    await acc_update(table, pk_col, pk_val, {"documents": json.dumps(new)}, user, label)
    return {"message": "Attachment deleted"}


# ── Email operations ────────────────────────────────────────────

async def acc_email_get(
    table: str, pk_col: str, pk_val: Any, user: UserContext, label: str = "Record",
) -> dict:
    rec = await acc_get(table, pk_col, pk_val, user, label)
    return {
        "to_mail_ids": [],
        "cc_mail_ids": [],
        "subject": f"{label} #{rec.get(pk_col)}",
        "body": "",
    }


async def acc_email_send(
    table: str, pk_col: str, pk_val: Any,
    email_data: dict, user: UserContext, label: str = "Record",
) -> dict:
    """Record an email event (actual sending delegated to email service)."""
    import json, datetime
    rec = await acc_get(table, pk_col, pk_val, user, label)
    history = rec.get("email_history") or []
    entry = {
        "to": email_data.get("to_mail_ids", []),
        "cc": email_data.get("cc_mail_ids", []),
        "subject": email_data.get("subject", ""),
        "sent_at": datetime.datetime.utcnow().isoformat(),
        "sent_by": str(user.user_id),
    }
    history.append(entry)
    await acc_update(table, pk_col, pk_val, {"email_history": json.dumps(history)}, user, label)
    return {"message": f"{label} email recorded"}


async def acc_email_history(
    table: str, pk_col: str, pk_val: Any, user: UserContext, label: str = "Record",
) -> list[dict]:
    rec = await acc_get(table, pk_col, pk_val, user, label)
    return rec.get("email_history") or []


# ── Address updates ─────────────────────────────────────────────

async def acc_address_update(
    table: str, pk_col: str, pk_val: Any,
    address_type: str, address_data: dict, user: UserContext, label: str = "Record",
) -> dict:
    col = f"{address_type}_address"
    import json
    return await acc_update(table, pk_col, pk_val, {col: json.dumps(address_data)}, user, label)


# ── Templates ───────────────────────────────────────────────────

async def acc_templates_list(
    table: str, user: UserContext,
) -> list[dict]:
    """List available templates for a module."""
    params: list[Any] = []
    if user.is_branch_user:
        params.extend([user.owner_id, user.branch_id])
        clause = "user_id = $1 AND branch_id = $2"
    else:
        params.append(user.user_id)
        clause = "user_id = $1"
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"SELECT DISTINCT template_id FROM {table} WHERE {clause} AND template_id IS NOT NULL",
            *params,
        )
        return [{"template_id": str(r["template_id"])} for r in rows]


async def acc_template_update(
    table: str, pk_col: str, pk_val: Any,
    template_id: Any, user: UserContext, label: str = "Record",
) -> dict:
    return await acc_update(table, pk_col, pk_val, {"template_id": template_id}, user, label)


# ── Bulk operations ─────────────────────────────────────────────

async def acc_bulk_delete(
    table: str, pk_col: str, ids: list, user: UserContext, label: str = "Record",
) -> dict:
    count = 0
    for pk_val in ids:
        try:
            await acc_delete(table, pk_col, pk_val, user, label)
            count += 1
        except HTTPException:
            pass
    return {"message": f"{count} {label}(s) deleted"}


async def acc_bulk_update(
    table: str, pk_col: str, ids: list, data: dict, user: UserContext, label: str = "Record",
) -> dict:
    count = 0
    for pk_val in ids:
        try:
            await acc_update(table, pk_col, pk_val, data, user, label)
            count += 1
        except HTTPException:
            pass
    return {"message": f"{count} {label}(s) updated"}


# ── Sub-resource CRUD (refunds, payments applied, etc.) ─────────

async def acc_sub_list(
    sub_table: str, parent_col: str, parent_id: Any, user: UserContext,
) -> list[dict]:
    return (await acc_list(sub_table, user, filters={parent_col: parent_id}, per_page=200))["items"]


async def acc_sub_create(
    sub_table: str, parent_col: str, parent_id: Any,
    data: dict, user: UserContext,
) -> dict:
    data[parent_col] = parent_id
    return await acc_create(sub_table, data, user)


async def acc_sub_get(
    sub_table: str, pk_col: str, pk_val: Any, user: UserContext, label: str = "Record",
) -> dict:
    return await acc_get(sub_table, pk_col, pk_val, user, label)


async def acc_sub_update(
    sub_table: str, pk_col: str, pk_val: Any,
    data: dict, user: UserContext, label: str = "Record",
) -> dict:
    return await acc_update(sub_table, pk_col, pk_val, data, user, label)


async def acc_sub_delete(
    sub_table: str, pk_col: str, pk_val: Any, user: UserContext, label: str = "Record",
) -> dict:
    return await acc_delete(sub_table, pk_col, pk_val, user, label)
