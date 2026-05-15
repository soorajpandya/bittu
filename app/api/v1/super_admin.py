"""
Super-Admin API — single namespace for top-tier platform operations.

Prefix:   /super-admin
Audience: members of `platform_admin_users` (super_admin tier).

Every endpoint is gated by `require_platform_admin()`, which checks
`fn_is_platform_admin(user_id)`. The legacy RBAC permission map does NOT
apply here — platform-admin status is global and bypasses branch-scoped
RBAC entirely.

Capabilities:
  • Identity .................. /me
  • Platform admins ........... GET/POST/DELETE /admins
  • Auth users (Supabase) ..... GET /users, POST /users, PUT /users/{id},
                                POST /users/{id}/reset-password,
                                DELETE /users/{id}
  • Merchants/restaurants ..... GET /merchants, GET /merchants/{id}
  • Cache invalidation ........ POST /cache/invalidate
  • Stats ..................... GET /stats
"""
from __future__ import annotations

from typing import Any, Optional

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from pydantic import BaseModel, EmailStr, Field

from app.core.auth import UserContext, require_platform_admin
from app.core.config import get_settings
from app.core.database import get_connection
from app.core.logging import get_logger
from app.core.redis import cache_delete, cache_delete_pattern
from app.services.rbac_service import rbac_service

router = APIRouter(prefix="/super-admin", tags=["Super Admin"])
logger = get_logger(__name__)


# ── Supabase admin helpers (service-role) ────────────────────────────
def _supa_admin_headers() -> dict[str, str]:
    s = get_settings()
    return {
        "apikey": s.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {s.SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def _supa_admin_url(path: str) -> str:
    s = get_settings()
    return f"{s.SUPABASE_URL}/auth/v1/admin/{path.lstrip('/')}"


async def _supa_request(
    method: str, path: str, *, json: dict | None = None, params: dict | None = None
) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(
            method, _supa_admin_url(path),
            headers=_supa_admin_headers(), json=json, params=params,
        )
    if r.status_code >= 400:
        try:
            data = r.json()
            msg = data.get("msg") or data.get("error_description") or data.get("message") or r.text
        except Exception:
            msg = r.text
        raise HTTPException(status_code=r.status_code, detail=f"supabase_admin: {msg}")
    if r.status_code == 204 or not r.content:
        return {}
    return r.json()


# ── Schemas ──────────────────────────────────────────────────────────
class GrantAdminBody(BaseModel):
    user_id: Optional[str] = Field(None, description="Existing Supabase user UUID")
    email:   Optional[EmailStr] = Field(None, description="Look up user by email")
    notes:   Optional[str] = Field(None, max_length=500)


class CreateUserBody(BaseModel):
    email:    EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    name:     Optional[str] = Field(None, max_length=120)
    role:     Optional[str] = Field(
        None, description="Optional user_metadata.role (e.g. owner, super_admin)"
    )
    grant_super_admin: bool = Field(
        False, description="Also insert into platform_admin_users on success"
    )


class UpdateUserBody(BaseModel):
    email:    Optional[EmailStr] = None
    password: Optional[str] = Field(None, min_length=8, max_length=128)
    name:     Optional[str] = Field(None, max_length=120)
    ban:      Optional[bool] = Field(None, description="True to ban (sets ban_duration='876000h')")


class ResetPasswordBody(BaseModel):
    password: str = Field(..., min_length=8, max_length=128)


class CacheInvalidateBody(BaseModel):
    user_id: Optional[str] = None
    pattern: Optional[str] = Field(
        None, description="Redis key pattern (e.g. 'rbac_perms:*')"
    )


class SuspendMerchantBody(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)


class MerchantNoteBody(BaseModel):
    note: str = Field(..., min_length=1, max_length=4000)


# ── Identity ─────────────────────────────────────────────────────────
@router.get("/me")
async def super_admin_me(
    user: UserContext = Depends(require_platform_admin()),
):
    """Confirm caller is a platform admin and echo identity + scope summary."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, email, notes, created_at "
            "FROM platform_admin_users WHERE user_id = $1::uuid",
            user.user_id,
        )
    return {
        "user_id":       user.user_id,
        "email":         user.email,
        "is_super_admin": True,
        "platform_admin_record": dict(row) if row else None,
        "scopes": [
            "platform:*",
            "financial:ledger:read",
            "financial:journal:post",
            "financial:payout:orchestrate",
            "financial:refund:orchestrate",
        ],
    }


# ── Platform admin management ────────────────────────────────────────
@router.get("/admins")
async def list_admins(
    _: UserContext = Depends(require_platform_admin()),
):
    async with get_connection() as conn:
        rows = await conn.fetch(
            "SELECT user_id, email, notes, created_at, created_by "
            "FROM platform_admin_users ORDER BY created_at DESC"
        )
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@router.post("/admins", status_code=201)
async def grant_admin(
    body: GrantAdminBody,
    actor: UserContext = Depends(require_platform_admin()),
):
    if not body.user_id and not body.email:
        raise HTTPException(400, "Provide user_id or email")

    async with get_connection() as conn:
        if body.user_id:
            target = await conn.fetchrow(
                "SELECT id, email FROM auth.users WHERE id = $1::uuid", body.user_id
            )
        else:
            target = await conn.fetchrow(
                "SELECT id, email FROM auth.users WHERE lower(email) = lower($1)",
                body.email,
            )
        if not target:
            raise HTTPException(404, "User not found in auth.users")

        await conn.execute(
            """
            INSERT INTO platform_admin_users (user_id, email, notes, created_by)
            VALUES ($1::uuid, $2, $3, $4::uuid)
            ON CONFLICT (user_id) DO UPDATE
              SET email = EXCLUDED.email,
                  notes = COALESCE(EXCLUDED.notes, platform_admin_users.notes)
            """,
            target["id"], target["email"], body.notes, actor.user_id,
        )
        row = await conn.fetchrow(
            "SELECT user_id, email, notes, created_at, created_by "
            "FROM platform_admin_users WHERE user_id = $1::uuid",
            target["id"],
        )

    # Bust caches so the grant takes effect on the next request.
    await _invalidate_user_caches(str(target["id"]))
    logger.info("super_admin_granted", actor=actor.user_id, target=str(target["id"]))
    return dict(row)


@router.delete("/admins/{user_id}", status_code=204)
async def revoke_admin(
    user_id: str = Path(..., description="Supabase user UUID"),
    actor: UserContext = Depends(require_platform_admin()),
):
    if user_id == actor.user_id:
        raise HTTPException(400, "Cannot revoke your own super-admin status")
    async with get_connection() as conn:
        result = await conn.execute(
            "DELETE FROM platform_admin_users WHERE user_id = $1::uuid", user_id
        )
    await _invalidate_user_caches(user_id)
    logger.info("super_admin_revoked", actor=actor.user_id, target=user_id, result=result)
    return None


# ── Auth users (Supabase) ────────────────────────────────────────────
@router.get("/users")
async def list_users(
    q:     Optional[str] = Query(None, description="Substring match on email"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: UserContext = Depends(require_platform_admin()),
):
    """List/search Supabase auth users joined with their restaurant + admin status."""
    sql = """
        SELECT u.id::text             AS user_id,
               u.email,
               u.created_at,
               u.last_sign_in_at,
               u.banned_until,
               u.email_confirmed_at,
               (pa.user_id IS NOT NULL) AS is_platform_admin,
               r.id::text             AS restaurant_id,
               r.name                 AS restaurant_name
          FROM auth.users u
          LEFT JOIN platform_admin_users pa ON pa.user_id = u.id
          LEFT JOIN restaurants r           ON r.owner_id::text = u.id::text
         WHERE ($1::text IS NULL OR u.email ILIKE '%' || $1 || '%')
         ORDER BY u.created_at DESC
         LIMIT $2 OFFSET $3
    """
    async with get_connection() as conn:
        rows = await conn.fetch(sql, q, limit, offset)
        total = await conn.fetchval(
            "SELECT count(*) FROM auth.users WHERE $1::text IS NULL OR email ILIKE '%' || $1 || '%'",
            q,
        )
    items = []
    for r in rows:
        d = dict(r)
        # Frontend-friendly aliases (kept alongside originals).
        d["is_admin"]  = d.get("is_platform_admin", False)
        d["is_banned"] = bool(d.get("banned_until"))
        items.append(d)
    return {"items": items, "limit": limit, "offset": offset, "total": total}


@router.get("/users/{user_id}")
async def get_user(
    user_id: str,
    _: UserContext = Depends(require_platform_admin()),
):
    async with get_connection() as conn:
        u = await conn.fetchrow(
            """
            SELECT u.id::text AS user_id, u.email, u.created_at,
                   u.last_sign_in_at, u.email_confirmed_at, u.banned_until,
                   u.raw_user_meta_data, u.raw_app_meta_data,
                   (pa.user_id IS NOT NULL) AS is_platform_admin
              FROM auth.users u
              LEFT JOIN platform_admin_users pa ON pa.user_id = u.id
             WHERE u.id = $1::uuid
            """,
            user_id,
        )
        if not u:
            raise HTTPException(404, "User not found")
        restaurants = await conn.fetch(
            "SELECT id::text, name, created_at FROM restaurants WHERE owner_id::text = $1",
            user_id,
        )
        branch_memberships = await conn.fetch(
            """
            SELECT bu.branch_id::text, sb.name AS branch_name,
                   bu.owner_id::text, bu.role_name, bu.is_active
              FROM branch_users bu
              LEFT JOIN sub_branches sb ON sb.id = bu.branch_id
             WHERE bu.user_id::text = $1
            """,
            user_id,
        )
    return {
        **dict(u),
        "restaurants":        [dict(r) for r in restaurants],
        "branch_memberships": [dict(b) for b in branch_memberships],
    }


@router.post("/users", status_code=201)
async def create_user(
    body: CreateUserBody,
    actor: UserContext = Depends(require_platform_admin()),
):
    """Create a Supabase auth user (email confirmed) and optionally make them a super admin."""
    payload: dict[str, Any] = {
        "email":         body.email,
        "password":      body.password,
        "email_confirm": True,
    }
    meta: dict[str, Any] = {}
    if body.name:
        meta["name"] = body.name
    if body.role:
        meta["role"] = body.role
    if meta:
        payload["user_metadata"] = meta

    created = await _supa_request("POST", "users", json=payload)
    new_uid = created.get("id") or (created.get("user") or {}).get("id")
    if not new_uid:
        raise HTTPException(502, f"Supabase admin returned no user id: {created}")

    if body.grant_super_admin:
        async with get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO platform_admin_users (user_id, email, notes, created_by)
                VALUES ($1::uuid, $2, $3, $4::uuid)
                ON CONFLICT (user_id) DO NOTHING
                """,
                new_uid, body.email, "Created via /super-admin/users", actor.user_id,
            )

    logger.info(
        "super_admin_created_user",
        actor=actor.user_id, new_user=new_uid, email=body.email,
        granted_admin=body.grant_super_admin,
    )
    return {
        "user_id":         new_uid,
        "email":           body.email,
        "is_platform_admin": body.grant_super_admin,
    }


@router.put("/users/{user_id}")
async def update_user(
    user_id: str,
    body: UpdateUserBody,
    actor: UserContext = Depends(require_platform_admin()),
):
    payload: dict[str, Any] = {}
    if body.email is not None:
        payload["email"] = body.email
    if body.password is not None:
        payload["password"] = body.password
    if body.name is not None:
        payload["user_metadata"] = {"name": body.name}
    if body.ban is True:
        payload["ban_duration"] = "876000h"  # ~100y
    elif body.ban is False:
        payload["ban_duration"] = "none"

    if not payload:
        raise HTTPException(400, "No fields to update")

    updated = await _supa_request("PUT", f"users/{user_id}", json=payload)
    await _invalidate_user_caches(user_id)
    logger.info("super_admin_updated_user", actor=actor.user_id, target=user_id, fields=list(payload.keys()))
    return updated


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    body: ResetPasswordBody,
    actor: UserContext = Depends(require_platform_admin()),
):
    await _supa_request("PUT", f"users/{user_id}", json={
        "password": body.password, "email_confirm": True,
    })
    await _invalidate_user_caches(user_id)
    logger.warning("super_admin_password_reset", actor=actor.user_id, target=user_id)
    return {"ok": True}


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    actor: UserContext = Depends(require_platform_admin()),
):
    if user_id == actor.user_id:
        raise HTTPException(400, "Cannot delete your own account")
    await _supa_request("DELETE", f"users/{user_id}")
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM platform_admin_users WHERE user_id = $1::uuid", user_id
        )
    await _invalidate_user_caches(user_id)
    logger.warning("super_admin_deleted_user", actor=actor.user_id, target=user_id)
    return None


# ── Merchants ────────────────────────────────────────────────────────
@router.get("/merchants")
async def list_merchants(
    q:     Optional[str] = Query(None, description="Substring match on name or owner email"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: UserContext = Depends(require_platform_admin()),
):
    sql = """
        SELECT r.id::text         AS restaurant_id,
               r.name,
               r.owner_id::text   AS owner_id,
               u.email            AS owner_email,
               r.created_at,
               (SELECT count(*) FROM sub_branches sb WHERE sb.restaurant_id = r.id) AS branch_count,
               (SELECT count(*) FROM branch_users bu WHERE bu.owner_id::text = r.owner_id::text) AS staff_count
          FROM restaurants r
          LEFT JOIN auth.users u ON u.id::text = r.owner_id::text
         WHERE ($1::text IS NULL
                OR r.name ILIKE '%' || $1 || '%'
                OR u.email ILIKE '%' || $1 || '%')
         ORDER BY r.created_at DESC
         LIMIT $2 OFFSET $3
    """
    async with get_connection() as conn:
        rows  = await conn.fetch(sql, q, limit, offset)
        total = await conn.fetchval(
            "SELECT count(*) FROM restaurants r LEFT JOIN auth.users u ON u.id::text = r.owner_id::text "
            "WHERE $1::text IS NULL OR r.name ILIKE '%' || $1 || '%' OR u.email ILIKE '%' || $1 || '%'",
            q,
        )
    return {"items": [dict(r) for r in rows], "limit": limit, "offset": offset, "total": total}


@router.get("/merchants/{restaurant_id}")
async def get_merchant(
    restaurant_id: str,
    _: UserContext = Depends(require_platform_admin()),
):
    async with get_connection() as conn:
        r = await conn.fetchrow(
            """
            SELECT r.id::text AS restaurant_id, r.name, r.owner_id::text AS owner_id,
                   u.email AS owner_email, r.created_at, r.updated_at,
                   r.suspended_at, r.suspended_reason,
                   r.suspended_by::text AS suspended_by,
                   sb_admin.email          AS suspended_by_email
              FROM restaurants r
              LEFT JOIN auth.users u        ON u.id::text = r.owner_id::text
              LEFT JOIN auth.users sb_admin ON sb_admin.id = r.suspended_by
             WHERE r.id = $1::uuid
            """,
            restaurant_id,
        )
        if not r:
            raise HTTPException(404, "Restaurant not found")
        branches = await conn.fetch(
            "SELECT id::text, name, is_main_branch, created_at "
            "FROM sub_branches WHERE restaurant_id = $1::uuid ORDER BY created_at",
            restaurant_id,
        )
        wallet = await conn.fetchrow(
            "SELECT current_balance, currency, last_posted_at "
            "FROM merchant_ledger_balance_locks WHERE merchant_id = $1::uuid",
            restaurant_id,
        )
    out = dict(r)
    out["is_suspended"] = out.get("suspended_at") is not None
    return {
        **out,
        "branches": [dict(b) for b in branches],
        "wallet":   dict(wallet) if wallet else None,
    }


# ── Merchant operational suspension ──────────────────────────────────
@router.post("/merchants/{restaurant_id}/suspend")
async def suspend_merchant(
    restaurant_id: str,
    body: SuspendMerchantBody,
    actor: UserContext = Depends(require_platform_admin()),
):
    """
    Operational suspension — blocks orders/payouts at the application
    layer (enforcement lives in those services). Distinct from KYC
    compliance suspension.
    """
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE restaurants
               SET suspended_at     = COALESCE(suspended_at, now()),
                   suspended_reason = $2,
                   suspended_by     = $3::uuid,
                   updated_at       = now()
             WHERE id = $1::uuid
         RETURNING id::text       AS restaurant_id,
                   suspended_at,
                   suspended_reason,
                   suspended_by::text AS suspended_by
            """,
            restaurant_id, body.reason, actor.user_id,
        )
        if not row:
            raise HTTPException(404, "Restaurant not found")
        # Best-effort note for audit trail.
        await conn.execute(
            """
            INSERT INTO merchant_admin_notes
                (merchant_id, note, author_id, author_email)
            VALUES ($1::uuid, $2, $3::uuid, $4)
            """,
            restaurant_id,
            f"[suspended] {body.reason}",
            actor.user_id, actor.email,
        )
    logger.warning("super_admin_suspend_merchant",
                   actor=actor.user_id, target=restaurant_id, reason=body.reason)
    return dict(row)


@router.post("/merchants/{restaurant_id}/unsuspend")
async def unsuspend_merchant(
    restaurant_id: str,
    actor: UserContext = Depends(require_platform_admin()),
):
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE restaurants
               SET suspended_at     = NULL,
                   suspended_reason = NULL,
                   suspended_by     = NULL,
                   updated_at       = now()
             WHERE id = $1::uuid
         RETURNING id::text       AS restaurant_id,
                   suspended_at,
                   suspended_reason,
                   suspended_by::text AS suspended_by
            """,
            restaurant_id,
        )
        if not row:
            raise HTTPException(404, "Restaurant not found")
        await conn.execute(
            """
            INSERT INTO merchant_admin_notes
                (merchant_id, note, author_id, author_email)
            VALUES ($1::uuid, $2, $3::uuid, $4)
            """,
            restaurant_id, "[unsuspended]", actor.user_id, actor.email,
        )
    logger.warning("super_admin_unsuspend_merchant",
                   actor=actor.user_id, target=restaurant_id)
    return dict(row)


# ── Merchant internal notes ──────────────────────────────────────────
@router.get("/merchants/{restaurant_id}/notes")
async def list_merchant_notes(
    restaurant_id: str,
    limit:  int = Query(50, ge=1, le=500),
    offset: int = Query(0,  ge=0),
    _: UserContext = Depends(require_platform_admin()),
):
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, note, author_id::text AS author_id,
                   author_email, created_at
              FROM merchant_admin_notes
             WHERE merchant_id = $1::uuid
             ORDER BY created_at DESC
             LIMIT $2 OFFSET $3
            """,
            restaurant_id, limit, offset,
        )
        total = await conn.fetchval(
            "SELECT count(*) FROM merchant_admin_notes WHERE merchant_id = $1::uuid",
            restaurant_id,
        )
    return {
        "items":  [dict(r) for r in rows],
        "limit":  limit,
        "offset": offset,
        "total":  total,
    }


@router.post("/merchants/{restaurant_id}/notes", status_code=201)
async def add_merchant_note(
    restaurant_id: str,
    body: MerchantNoteBody,
    actor: UserContext = Depends(require_platform_admin()),
):
    async with get_connection() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM restaurants WHERE id = $1::uuid", restaurant_id
        )
        if not exists:
            raise HTTPException(404, "Restaurant not found")
        row = await conn.fetchrow(
            """
            INSERT INTO merchant_admin_notes
                (merchant_id, note, author_id, author_email)
            VALUES ($1::uuid, $2, $3::uuid, $4)
         RETURNING id, note, author_id::text AS author_id,
                   author_email, created_at
            """,
            restaurant_id, body.note, actor.user_id, actor.email,
        )
    logger.info("super_admin_merchant_note_added",
                actor=actor.user_id, target=restaurant_id, note_id=row["id"])
    return dict(row)


# ── Cache invalidation ───────────────────────────────────────────────
@router.post("/cache/invalidate")
async def cache_invalidate(
    body: CacheInvalidateBody,
    actor: UserContext = Depends(require_platform_admin()),
):
    if not body.user_id and not body.pattern:
        raise HTTPException(400, "Provide user_id or pattern")
    cleared = []
    if body.user_id:
        await _invalidate_user_caches(body.user_id)
        cleared.append(f"user:{body.user_id}")
    if body.pattern:
        await cache_delete_pattern(body.pattern)
        cleared.append(f"pattern:{body.pattern}")
    logger.info("super_admin_cache_invalidate", actor=actor.user_id, cleared=cleared)
    return {"ok": True, "cleared": cleared}


# ── Stats ────────────────────────────────────────────────────────────
@router.get("/stats")
async def stats(
    _: UserContext = Depends(require_platform_admin()),
):
    async with get_connection() as conn:
        users         = await conn.fetchval("SELECT count(*) FROM auth.users")
        admins        = await conn.fetchval("SELECT count(*) FROM platform_admin_users")
        restaurants   = await conn.fetchval("SELECT count(*) FROM restaurants")
        branches      = await conn.fetchval("SELECT count(*) FROM sub_branches")
        staff         = await conn.fetchval("SELECT count(*) FROM branch_users")
        ledger_total  = await conn.fetchval(
            "SELECT COALESCE(SUM(current_balance),0) FROM merchant_ledger_balance_locks WHERE currency='INR'"
        )
        active_24h    = await conn.fetchval(
            "SELECT count(*) FROM auth.users WHERE last_sign_in_at > now() - interval '24 hours'"
        )
        # Platform fee revenue last 30d, in INR (rupees) and paise.
        revenue_inr   = await conn.fetchval(
            """
            SELECT COALESCE(SUM(fee_amount), 0)::numeric(18,2)
              FROM fee_computations
             WHERE currency = 'INR'
               AND computed_at >= now() - interval '30 days'
            """
        )
        suspended     = await conn.fetchval(
            "SELECT count(*) FROM restaurants WHERE suspended_at IS NOT NULL"
        )
    revenue_inr_f     = float(revenue_inr or 0)
    return {
        # Original keys (unchanged for backwards compatibility).
        "users":              users,
        "platform_admins":    admins,
        "restaurants":        restaurants,
        "branches":           branches,
        "branch_staff":       staff,
        "merchant_ledger_total_inr": float(ledger_total or 0),
        "active_users_24h":   active_24h,
        # Frontend-friendly aliases.
        "total_users":             users,
        "total_admins":            admins,
        "total_merchants":         restaurants,
        "total_branches":          branches,
        "total_staff":             staff,
        "suspended_merchants":     suspended,
        "platform_revenue_inr":    revenue_inr_f,
        "total_revenue_inr_paise": int(round(revenue_inr_f * 100)),
    }


# ── Helpers ──────────────────────────────────────────────────────────
async def _invalidate_user_caches(user_id: str) -> None:
    """Best-effort: drop user_ctx + RBAC perms for a user."""
    try:
        await cache_delete(f"user_ctx:{user_id}")
    except Exception as e:  # pragma: no cover
        logger.warning("cache_invalidate_user_ctx_failed", user_id=user_id, error=str(e))
    try:
        await cache_delete_pattern(f"rbac_perms:{user_id}:*")
    except Exception as e:  # pragma: no cover
        logger.warning("cache_invalidate_rbac_failed", user_id=user_id, error=str(e))
    # In-process RBAC TTL cache + Redis perm cache
    try:
        await rbac_service.invalidate_user_cache(user_id=user_id, branch_id=None)
    except Exception:
        pass
