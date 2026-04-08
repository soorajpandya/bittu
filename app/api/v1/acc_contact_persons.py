"""Accounting Contact Persons CRUD endpoints."""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import UserContext, get_current_user, require_permission
from app.core.database import get_connection
from app.services.accounting_crud import acc_create, acc_update, acc_delete

router = APIRouter(prefix="/accounting/contacts", tags=["Accounting – Contact Persons"])

TABLE = "acc_contact_persons"
PK = "contact_person_id"
LABEL = "Contact Person"


_auth = require_permission("accounting:read")


class ContactPersonCreate(BaseModel):
    salutation: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    mobile: Optional[str] = None
    designation: Optional[str] = None
    department: Optional[str] = None
    is_primary_contact: bool = False


class ContactPersonUpdate(BaseModel):
    salutation: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    mobile: Optional[str] = None
    designation: Optional[str] = None
    department: Optional[str] = None
    is_primary_contact: Optional[bool] = None


@router.get("/{contact_id}/contactpersons")
async def list_contact_persons(
    contact_id: UUID,
    user: UserContext = Depends(_auth),
):
    params = []
    if user.is_branch_user:
        params.extend([user.owner_id, user.branch_id])
        clause = "t.user_id = $1 AND t.branch_id = $2"
    else:
        params.append(user.user_id)
        clause = "t.user_id = $1"
    params.append(contact_id)

    async with get_connection() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM {TABLE} t WHERE {clause} AND t.contact_id = ${len(params)} ORDER BY t.is_primary_contact DESC, t.created_at",
            *params,
        )
        return [dict(r) for r in rows]


@router.get("/{contact_id}/contactpersons/{contact_person_id}")
async def get_contact_person(
    contact_id: UUID,
    contact_person_id: UUID,
    user: UserContext = Depends(_auth),
):
    params = []
    if user.is_branch_user:
        params.extend([user.owner_id, user.branch_id])
        clause = "t.user_id = $1 AND t.branch_id = $2"
    else:
        params.append(user.user_id)
        clause = "t.user_id = $1"
    params.extend([contact_id, contact_person_id])

    async with get_connection() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {TABLE} t WHERE {clause} AND t.contact_id = ${len(params)-1} AND t.contact_person_id = ${len(params)}",
            *params,
        )
        if not row:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=LABEL + " not found")
        return dict(row)


@router.post("/{contact_id}/contactpersons")
async def create_contact_person(
    contact_id: UUID,
    body: ContactPersonCreate,
    user: UserContext = Depends(_auth),
):
    data = body.model_dump(exclude_none=True)
    data["contact_id"] = contact_id
    return await acc_create(TABLE, data, user)


@router.put("/{contact_id}/contactpersons/{contact_person_id}")
async def update_contact_person(
    contact_id: UUID,
    contact_person_id: UUID,
    body: ContactPersonUpdate,
    user: UserContext = Depends(_auth),
):
    return await acc_update(TABLE, PK, contact_person_id, body.model_dump(exclude_unset=True, exclude_none=True), user, LABEL)


@router.delete("/{contact_id}/contactpersons/{contact_person_id}")
async def delete_contact_person(
    contact_id: UUID,
    contact_person_id: UUID,
    user: UserContext = Depends(_auth),
):
    return await acc_delete(TABLE, PK, contact_person_id, user, LABEL)
