"""
Fee Engine v2 — service layer (Phase 10).

Pluggable fee plans replace the hardcoded 0.30% in `statement_service`.
Existing settlement math is left untouched; this engine is opt-in for new
code paths and exposes a deterministic `compute_fee` / `preview_fee` API.

Conventions match Phase 7-9 services: thin wrappers around SQL fns, row
mappers normalize JSONB columns, all timestamps to ISO strings.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from app.core.database import get_connection
from app.core.exceptions import (
    NotFoundError, ValidationError, ConflictError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

_FEE_TYPES = {"percent", "flat", "percent_plus_flat"}


def _to_dec(v) -> Optional[str]:
    return str(v) if v is not None else None


def _plan_row(r) -> dict:
    if r is None:
        return {}
    meta = r["metadata"]
    if isinstance(meta, str): meta = json.loads(meta)
    return {
        "id":          int(r["id"]),
        "plan_uuid":   str(r["plan_uuid"]),
        "code":        r["code"],
        "name":        r["name"],
        "description": r["description"],
        "currency":    r["currency"],
        "gst_rate":    str(r["gst_rate"]),
        "is_active":   bool(r["is_active"]),
        "is_default":  bool(r["is_default"]),
        "valid_from":  r["valid_from"].isoformat(),
        "valid_to":    r["valid_to"].isoformat() if r["valid_to"] else None,
        "metadata":    meta or {},
        "created_at":  r["created_at"].isoformat(),
        "updated_at":  r["updated_at"].isoformat(),
    }


def _rule_row(r) -> dict:
    meta = r["metadata"]
    if isinstance(meta, str): meta = json.loads(meta)
    return {
        "id":             int(r["id"]),
        "rule_uuid":      str(r["rule_uuid"]),
        "plan_id":        int(r["plan_id"]),
        "payment_method": r["payment_method"],
        "order_source":   r["order_source"],
        "min_amount":     str(r["min_amount"]),
        "max_amount":     _to_dec(r["max_amount"]),
        "fee_type":       r["fee_type"],
        "percent_rate":   str(r["percent_rate"]),
        "flat_fee":       str(r["flat_fee"]),
        "priority":       int(r["priority"]),
        "is_active":      bool(r["is_active"]),
        "metadata":       meta or {},
        "created_at":     r["created_at"].isoformat(),
        "updated_at":     r["updated_at"].isoformat(),
    }


def _override_row(r) -> dict:
    meta = r["metadata"]
    if isinstance(meta, str): meta = json.loads(meta)
    return {
        "id":            int(r["id"]),
        "override_uuid": str(r["override_uuid"]),
        "merchant_id":   str(r["merchant_id"]),
        "plan_id":       int(r["plan_id"]),
        "valid_from":    r["valid_from"].isoformat(),
        "valid_to":      r["valid_to"].isoformat() if r["valid_to"] else None,
        "reason":        r["reason"],
        "created_by_admin_id": str(r["created_by_admin_id"]) if r["created_by_admin_id"] else None,
        "metadata":      meta or {},
        "created_at":    r["created_at"].isoformat(),
        "updated_at":    r["updated_at"].isoformat(),
    }


def _comp_row(r) -> dict:
    bd = r["breakdown"]
    if isinstance(bd, str): bd = json.loads(bd)
    return {
        "id":              int(r["id"]),
        "computation_uuid": str(r["computation_uuid"]),
        "merchant_id":     str(r["merchant_id"]),
        "payment_id":      r["payment_id"],
        "plan_id":         int(r["plan_id"]),
        "rule_id":         int(r["rule_id"]) if r["rule_id"] else None,
        "payment_method":  r["payment_method"],
        "order_source":    r["order_source"],
        "currency":        r["currency"],
        "gross_amount":    str(r["gross_amount"]),
        "fee_amount":      str(r["fee_amount"]),
        "gst_amount":      str(r["gst_amount"]),
        "total_deduction": str(r["total_deduction"]),
        "net_amount":      str(r["net_amount"]),
        "breakdown":       bd or {},
        "computed_at":     r["computed_at"].isoformat(),
    }


class FeeService:
    # ╔══════════════════════════ plans ══════════════════════════════╗
    async def list_plans(
        self, *, active_only: bool = False, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        clauses, params = [], []
        if active_only:
            clauses.append("is_active = true")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])
        async with get_connection() as c:
            rows = await c.fetch(
                f"""
                SELECT * FROM fee_plans
                {where}
                ORDER BY is_default DESC, id
                LIMIT ${len(params) - 1} OFFSET ${len(params)}
                """,
                *params,
            )
        return [_plan_row(r) for r in rows]

    async def get_plan(self, plan_id: int) -> dict:
        async with get_connection() as c:
            r = await c.fetchrow("SELECT * FROM fee_plans WHERE id = $1", plan_id)
        if not r:
            raise NotFoundError("fee plan not found")
        return _plan_row(r)

    async def get_plan_by_code(self, code: str) -> dict:
        async with get_connection() as c:
            r = await c.fetchrow("SELECT * FROM fee_plans WHERE code = $1", code)
        if not r:
            raise NotFoundError("fee plan not found")
        return _plan_row(r)

    async def create_plan(
        self,
        *,
        code: str,
        name: str,
        description: Optional[str] = None,
        currency: str = "INR",
        gst_rate: Decimal | float = Decimal("0.18"),
        is_active: bool = True,
        is_default: bool = False,
        valid_from: Optional[str] = None,
        valid_to: Optional[str] = None,
        metadata: Optional[dict] = None,
        created_by_admin_id: Optional[str | UUID] = None,
    ) -> dict:
        if not code or not code.strip():
            raise ValidationError("code required")
        if not name or not name.strip():
            raise ValidationError("name required")
        gst_d = Decimal(str(gst_rate))
        if not (Decimal("0") <= gst_d <= Decimal("1")):
            raise ValidationError("gst_rate must be in [0,1]")

        async with get_connection() as c:
            if is_default:
                # demote existing default
                await c.execute(
                    "UPDATE fee_plans SET is_default = false WHERE is_default = true"
                )
            try:
                r = await c.fetchrow(
                    """
                    INSERT INTO fee_plans
                      (code, name, description, currency, gst_rate, is_active,
                       is_default, valid_from, valid_to, metadata, created_by_admin_id)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,
                            COALESCE($8::timestamptz, now()),
                            $9::timestamptz,
                            $10::jsonb,
                            $11::uuid)
                    RETURNING *
                    """,
                    code, name, description, currency, gst_d, is_active,
                    is_default, valid_from, valid_to,
                    json.dumps(metadata or {}),
                    str(created_by_admin_id) if created_by_admin_id else None,
                )
            except Exception as e:
                if "duplicate key" in str(e) or "fee_plans_code_key" in str(e):
                    raise ConflictError(f"plan code '{code}' already exists")
                raise
        return _plan_row(r)

    async def update_plan(
        self,
        plan_id: int,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        gst_rate: Optional[Decimal | float] = None,
        is_active: Optional[bool] = None,
        is_default: Optional[bool] = None,
        valid_to: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        sets, params = [], []
        def _add(col, val, cast=""):
            params.append(val)
            sets.append(f"{col} = ${len(params)}{cast}")

        if name        is not None: _add("name", name)
        if description is not None: _add("description", description)
        if gst_rate    is not None:
            gst_d = Decimal(str(gst_rate))
            if not (Decimal("0") <= gst_d <= Decimal("1")):
                raise ValidationError("gst_rate must be in [0,1]")
            _add("gst_rate", gst_d)
        if is_active   is not None: _add("is_active", is_active)
        if valid_to    is not None: _add("valid_to", valid_to, "::timestamptz")
        if metadata    is not None: _add("metadata", json.dumps(metadata), "::jsonb")

        async with get_connection() as c:
            if is_default is True:
                await c.execute(
                    "UPDATE fee_plans SET is_default = false "
                    "WHERE is_default = true AND id <> $1",
                    plan_id,
                )
                _add("is_default", True)
            elif is_default is False:
                _add("is_default", False)

            if not sets:
                r = await c.fetchrow("SELECT * FROM fee_plans WHERE id = $1", plan_id)
            else:
                params.append(plan_id)
                r = await c.fetchrow(
                    f"UPDATE fee_plans SET {', '.join(sets)} "
                    f"WHERE id = ${len(params)} RETURNING *",
                    *params,
                )
        if not r:
            raise NotFoundError("fee plan not found")
        return _plan_row(r)

    # ╔══════════════════════════ rules ══════════════════════════════╗
    async def list_rules(self, plan_id: int) -> list[dict]:
        async with get_connection() as c:
            rows = await c.fetch(
                "SELECT * FROM fee_plan_rules WHERE plan_id = $1 "
                "ORDER BY priority DESC, id",
                plan_id,
            )
        return [_rule_row(r) for r in rows]

    async def add_rule(
        self,
        plan_id: int,
        *,
        payment_method: Optional[str] = None,
        order_source: Optional[str] = None,
        min_amount: Decimal | float = 0,
        max_amount: Optional[Decimal | float] = None,
        fee_type: str = "percent",
        percent_rate: Decimal | float = 0,
        flat_fee: Decimal | float = 0,
        priority: int = 100,
        is_active: bool = True,
        metadata: Optional[dict] = None,
    ) -> dict:
        if fee_type not in _FEE_TYPES:
            raise ValidationError(f"fee_type must be one of {sorted(_FEE_TYPES)}")
        pr = Decimal(str(percent_rate))
        if not (Decimal("0") <= pr <= Decimal("1")):
            raise ValidationError("percent_rate must be in [0,1]")
        if Decimal(str(flat_fee)) < 0:
            raise ValidationError("flat_fee must be >= 0")
        if Decimal(str(min_amount)) < 0:
            raise ValidationError("min_amount must be >= 0")
        if max_amount is not None and Decimal(str(max_amount)) <= Decimal(str(min_amount)):
            raise ValidationError("max_amount must be > min_amount")

        # ensure plan exists
        await self.get_plan(plan_id)

        async with get_connection() as c:
            r = await c.fetchrow(
                """
                INSERT INTO fee_plan_rules
                  (plan_id, payment_method, order_source, min_amount, max_amount,
                   fee_type, percent_rate, flat_fee, priority, is_active, metadata)
                VALUES ($1,$2,$3,$4,$5,$6::fee_calc_type,$7,$8,$9,$10,$11::jsonb)
                RETURNING *
                """,
                plan_id, payment_method, order_source,
                Decimal(str(min_amount)),
                Decimal(str(max_amount)) if max_amount is not None else None,
                fee_type, pr, Decimal(str(flat_fee)), priority, is_active,
                json.dumps(metadata or {}),
            )
        return _rule_row(r)

    async def update_rule(
        self,
        rule_id: int,
        *,
        payment_method: Optional[str] = None,
        order_source: Optional[str] = None,
        min_amount: Optional[Decimal | float] = None,
        max_amount: Optional[Decimal | float] = None,
        fee_type: Optional[str] = None,
        percent_rate: Optional[Decimal | float] = None,
        flat_fee: Optional[Decimal | float] = None,
        priority: Optional[int] = None,
        is_active: Optional[bool] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        sets, params = [], []
        def _add(col, val, cast=""):
            params.append(val)
            sets.append(f"{col} = ${len(params)}{cast}")

        if payment_method is not None: _add("payment_method", payment_method or None)
        if order_source   is not None: _add("order_source", order_source or None)
        if min_amount     is not None: _add("min_amount", Decimal(str(min_amount)))
        if max_amount     is not None: _add("max_amount", Decimal(str(max_amount)))
        if fee_type       is not None:
            if fee_type not in _FEE_TYPES:
                raise ValidationError("invalid fee_type")
            _add("fee_type", fee_type, "::fee_calc_type")
        if percent_rate   is not None:
            pr = Decimal(str(percent_rate))
            if not (Decimal("0") <= pr <= Decimal("1")):
                raise ValidationError("percent_rate must be in [0,1]")
            _add("percent_rate", pr)
        if flat_fee       is not None: _add("flat_fee", Decimal(str(flat_fee)))
        if priority       is not None: _add("priority", priority)
        if is_active      is not None: _add("is_active", is_active)
        if metadata       is not None: _add("metadata", json.dumps(metadata), "::jsonb")

        if not sets:
            return (await self.get_rule(rule_id))

        params.append(rule_id)
        async with get_connection() as c:
            r = await c.fetchrow(
                f"UPDATE fee_plan_rules SET {', '.join(sets)} "
                f"WHERE id = ${len(params)} RETURNING *",
                *params,
            )
        if not r:
            raise NotFoundError("fee rule not found")
        return _rule_row(r)

    async def get_rule(self, rule_id: int) -> dict:
        async with get_connection() as c:
            r = await c.fetchrow("SELECT * FROM fee_plan_rules WHERE id = $1", rule_id)
        if not r:
            raise NotFoundError("fee rule not found")
        return _rule_row(r)

    async def remove_rule(self, rule_id: int) -> None:
        async with get_connection() as c:
            res = await c.execute("DELETE FROM fee_plan_rules WHERE id = $1", rule_id)
        # res like "DELETE 1"
        if res.endswith(" 0"):
            raise NotFoundError("fee rule not found")

    # ╔══════════════════════════ overrides ══════════════════════════╗
    async def set_merchant_override(
        self,
        merchant_id: str | UUID,
        *,
        plan_id: int,
        valid_from: Optional[str] = None,
        valid_to: Optional[str] = None,
        reason: Optional[str] = None,
        created_by_admin_id: Optional[str | UUID] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        await self.get_plan(plan_id)
        async with get_connection() as c:
            r = await c.fetchrow(
                """
                INSERT INTO merchant_fee_overrides
                  (merchant_id, plan_id, valid_from, valid_to, reason,
                   created_by_admin_id, metadata)
                VALUES ($1::uuid, $2,
                        COALESCE($3::timestamptz, now()),
                        $4::timestamptz,
                        $5, $6::uuid, $7::jsonb)
                RETURNING *
                """,
                str(merchant_id), plan_id, valid_from, valid_to, reason,
                str(created_by_admin_id) if created_by_admin_id else None,
                json.dumps(metadata or {}),
            )
        return _override_row(r)

    async def list_overrides(
        self,
        merchant_id: Optional[str | UUID] = None,
        *,
        active_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses, params = [], []
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        if active_only:
            clauses.append(
                "valid_from <= now() AND (valid_to IS NULL OR valid_to > now())"
            )
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])
        async with get_connection() as c:
            rows = await c.fetch(
                f"""
                SELECT * FROM merchant_fee_overrides
                {where}
                ORDER BY valid_from DESC, id DESC
                LIMIT ${len(params) - 1} OFFSET ${len(params)}
                """,
                *params,
            )
        return [_override_row(r) for r in rows]

    async def end_override(
        self,
        override_id: int,
        *,
        valid_to: Optional[str] = None,
    ) -> dict:
        async with get_connection() as c:
            r = await c.fetchrow(
                """
                UPDATE merchant_fee_overrides
                   SET valid_to = COALESCE($1::timestamptz, now())
                 WHERE id = $2
                 RETURNING *
                """,
                valid_to, override_id,
            )
        if not r:
            raise NotFoundError("override not found")
        return _override_row(r)

    # ╔══════════════════════════ resolve / compute ══════════════════╗
    async def resolve_plan(
        self, merchant_id: str | UUID, *, at_ts: Optional[str] = None,
    ) -> dict:
        async with get_connection() as c:
            plan_id = await c.fetchval(
                "SELECT fn_resolve_fee_plan($1::uuid, COALESCE($2::timestamptz, now()))",
                str(merchant_id), at_ts,
            )
        if not plan_id:
            raise NotFoundError("no fee plan resolvable")
        return await self.get_plan(int(plan_id))

    async def compute_fee(
        self,
        merchant_id: str | UUID,
        *,
        gross: Decimal | float,
        payment_method: Optional[str] = None,
        order_source: Optional[str] = None,
        currency: str = "INR",
        at_ts: Optional[str] = None,
        record: bool = False,
        payment_id: Optional[str] = None,
    ) -> dict:
        gd = Decimal(str(gross))
        if gd < 0:
            raise ValidationError("gross must be >= 0")
        try:
            async with get_connection() as c:
                j = await c.fetchval(
                    """
                    SELECT fn_compute_fee(
                      $1::uuid, $2::numeric, $3, $4, $5::char(3),
                      COALESCE($6::timestamptz, now()), $7, $8
                    )
                    """,
                    str(merchant_id), gd, payment_method, order_source,
                    currency, at_ts, record, payment_id,
                )
        except Exception as e:
            msg = str(e)
            if "no fee plan" in msg:
                raise NotFoundError(msg)
            if "gross must be" in msg:
                raise ValidationError(msg)
            raise
        if not j:
            return {}
        # Coerce all numerics to Decimal — asyncpg may decode JSONB
        # numbers as floats which loses precision (2.54 → 2.5400…035).
        if not isinstance(j, str):
            j = json.dumps(j)
        return json.loads(j, parse_float=Decimal, parse_int=Decimal)

    async def preview_fee(
        self,
        merchant_id: str | UUID,
        *,
        gross: Decimal | float,
        payment_method: Optional[str] = None,
        order_source: Optional[str] = None,
        currency: str = "INR",
        at_ts: Optional[str] = None,
    ) -> dict:
        return await self.compute_fee(
            merchant_id, gross=gross, payment_method=payment_method,
            order_source=order_source, currency=currency, at_ts=at_ts,
            record=False,
        )

    # ╔══════════════════════════ computations log ═══════════════════╗
    async def list_computations(
        self,
        merchant_id: Optional[str | UUID] = None,
        *,
        payment_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses, params = [], []
        if merchant_id is not None:
            params.append(str(merchant_id))
            clauses.append(f"merchant_id = ${len(params)}::uuid")
        if payment_id is not None:
            params.append(payment_id)
            clauses.append(f"payment_id = ${len(params)}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])
        async with get_connection() as c:
            rows = await c.fetch(
                f"""
                SELECT * FROM fee_computations
                {where}
                ORDER BY id DESC
                LIMIT ${len(params) - 1} OFFSET ${len(params)}
                """,
                *params,
            )
        return [_comp_row(r) for r in rows]


fee_service = FeeService()
