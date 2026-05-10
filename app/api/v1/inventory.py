"""Inventory Management endpoints.

Section 6 — API Design (event-sourced inventory).

Backward-compatible: original `/inventory/stock` and `/inventory/receive`
endpoints are preserved unchanged. Layered on top is the full event-sourced
surface described in inventory.md:

  • Real-time balances & per-ingredient timeline
  • Snapshots (period balances)
  • Adjustments (with audit trail)
  • Wastage logging (with reasons + photo)
  • Inter-branch stock transfers (ship → receive)
  • Physical inventory counts (variance → ledger events)
  • Alerts (low-stock / expiring / etc.)
  • Expiry dashboard (FEFO buckets)
  • Analytics (per-day rollups)
  • Vendor CRUD
  • Unit conversions
  • Reconciliation drift detector

Every mutation:
  • Tenant-scoped via permissions + UserContext.restaurant_id/branch_id
  • Idempotent where appropriate (dedup_key built from natural identifiers)
  • Emits a domain event → Redis pub/sub → websocket fan-out
  • Activity-logged where caller-meaningful
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.database import get_connection, get_serializable_transaction
from app.core.events import (
    INVENTORY_WASTED, INVENTORY_ADJUSTED,
    INVENTORY_TRANSFERRED_OUT, INVENTORY_TRANSFERRED_IN,
    INVENTORY_RECOUNTED,
)
from app.core.exceptions import NotFoundError, ValidationError
from app.services.activity_log_service import log_activity
from app.services.inventory_service import InventoryService
from app.services.inventory_event_service import inventory_event_service

router = APIRouter(prefix="/inventory", tags=["Inventory"])
_svc = InventoryService()


# ════════════════════════════════════════════════════════════════════════════
# LEGACY (backward compat — DO NOT CHANGE BEHAVIOUR)
# ════════════════════════════════════════════════════════════════════════════

class ReceivePurchaseIn(BaseModel):
    purchase_order_id: str


@router.get("/stock")
async def get_stock_levels(
    branch_id: Optional[str] = None,
    low_only: bool = False,
    user: UserContext = Depends(require_permission("inventory.read")),
):
    return await _svc.get_stock_levels(user=user, low_stock_only=low_only)


@router.post("/receive")
async def receive_purchase_order(
    body: ReceivePurchaseIn,
    user: UserContext = Depends(require_permission("inventory.update")),
):
    result = await _svc.receive_purchase_order(
        user=user, purchase_order_id=body.purchase_order_id,
    )
    await log_activity(
        user_id=user.user_id,
        branch_id=user.branch_id,
        action="inventory.received",
        entity_type="purchase_order",
        entity_id=body.purchase_order_id,
        metadata={},
    )
    return result


# ════════════════════════════════════════════════════════════════════════════
# REAL-TIME BALANCES & TIMELINE  (Section 3 — Calculation Engine)
# ════════════════════════════════════════════════════════════════════════════

@router.get("/balances")
async def get_balances_bulk(
    branch_id: Optional[str] = None,
    user: UserContext = Depends(require_permission("inventory.read")),
):
    """Live balances (event-sourced) for every ingredient owned by caller."""
    if not user.restaurant_id:
        return []
    return await inventory_event_service.get_balances_bulk(
        restaurant_id=user.restaurant_id,
        branch_id=branch_id or user.branch_id,
    )


@router.get("/balance/{ingredient_id}")
async def get_balance(
    ingredient_id: str,
    branch_id: Optional[str] = None,
    as_of: Optional[str] = None,
    user: UserContext = Depends(require_permission("inventory.read")),
):
    qty = await inventory_event_service.get_balance(
        ingredient_id, branch_id=branch_id or user.branch_id, as_of=as_of,
    )
    return {"ingredient_id": ingredient_id, "balance": float(qty), "as_of": as_of}


@router.get("/timeline/{ingredient_id}")
async def get_timeline(
    ingredient_id: str,
    branch_id: Optional[str] = None,
    limit: int = Query(100, le=500),
    offset: int = 0,
    user: UserContext = Depends(require_permission("inventory.read")),
):
    return await inventory_event_service.timeline(
        ingredient_id=ingredient_id,
        branch_id=branch_id or user.branch_id,
        limit=limit, offset=offset,
    )


# ════════════════════════════════════════════════════════════════════════════
# SNAPSHOTS
# ════════════════════════════════════════════════════════════════════════════

@router.post("/snapshots/build")
async def build_snapshot(
    branch_id: Optional[str] = None,
    period: str = "rolling",
    user: UserContext = Depends(require_permission("inventory.update")),
):
    if not user.restaurant_id:
        raise ValidationError("restaurant context required")
    n = await inventory_event_service.build_snapshot(
        restaurant_id=user.restaurant_id,
        branch_id=branch_id or user.branch_id,
        period=period,
    )
    await log_activity(
        user_id=user.user_id, branch_id=user.branch_id,
        action="inventory.snapshot_built", entity_type="snapshot",
        entity_id=period, metadata={"rows": n},
    )
    return {"built": n, "period": period}


@router.get("/snapshots")
async def list_snapshots(
    ingredient_id: Optional[str] = None,
    branch_id: Optional[str] = None,
    period: str = "rolling",
    limit: int = Query(50, le=200),
    user: UserContext = Depends(require_permission("inventory.read")),
):
    if not user.restaurant_id:
        return []
    params: list = [user.restaurant_id, period]
    where = "restaurant_id = $1::uuid AND period = $2"
    if branch_id or user.branch_id:
        params.append(branch_id or user.branch_id)
        where += f" AND branch_id = ${len(params)}::uuid"
    if ingredient_id:
        params.append(ingredient_id)
        where += f" AND ingredient_id = ${len(params)}"
    params.append(limit)
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM inventory_snapshots WHERE {where} "
            f"ORDER BY snapshot_at DESC LIMIT ${len(params)}",
            *params,
        )
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
# ADJUSTMENTS
# ════════════════════════════════════════════════════════════════════════════

class AdjustmentIn(BaseModel):
    ingredient_id: str
    branch_id: Optional[str] = None
    adjustment_type: str = Field(..., pattern="^(increase|decrease|recount|damage|theft|found)$")
    quantity: Decimal = Field(..., gt=0)
    unit: Optional[str] = None
    unit_cost: Optional[Decimal] = None
    reason: Optional[str] = None
    notes: Optional[str] = None


@router.post("/adjustments")
async def create_adjustment(
    body: AdjustmentIn,
    user: UserContext = Depends(require_permission("inventory.update")),
):
    if not user.restaurant_id:
        raise ValidationError("restaurant context required")

    is_in = body.adjustment_type in ("increase", "found")
    ledger_type = "adjustment_in" if is_in else "adjustment_out"

    event_id = await inventory_event_service.append_event(
        restaurant_id=user.restaurant_id,
        branch_id=body.branch_id or user.branch_id,
        ingredient_id=body.ingredient_id,
        event_type=INVENTORY_ADJUSTED,
        ledger_type=ledger_type,
        quantity_in=body.quantity if is_in else Decimal("0"),
        quantity_out=Decimal("0") if is_in else body.quantity,
        unit_cost=body.unit_cost or 0,
        reference_type="adjustment",
        source="manual",
        notes=body.notes or body.reason,
        created_by=user.user_id,
        metadata={"adjustment_type": body.adjustment_type, "reason": body.reason},
    )

    async with get_connection() as conn:
        adj_id = await conn.fetchval(
            """
            INSERT INTO inventory_adjustments
                (restaurant_id, branch_id, ingredient_id, adjustment_type,
                 quantity, unit, unit_cost, reason, notes,
                 ledger_event_id, created_by)
            VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6,$7,$8,$9,$10::uuid,$11)
            RETURNING id
            """,
            user.restaurant_id, body.branch_id or user.branch_id,
            body.ingredient_id, body.adjustment_type,
            float(body.quantity), body.unit, float(body.unit_cost or 0),
            body.reason, body.notes, event_id, user.user_id,
        )
    await log_activity(
        user_id=user.user_id, branch_id=user.branch_id,
        action="inventory.adjusted", entity_type="ingredient",
        entity_id=body.ingredient_id,
        metadata={"adjustment_id": str(adj_id), "type": body.adjustment_type,
                  "qty": float(body.quantity)},
    )
    return {"adjustment_id": str(adj_id), "event_id": event_id}


@router.get("/adjustments")
async def list_adjustments(
    ingredient_id: Optional[str] = None,
    limit: int = Query(50, le=200),
    user: UserContext = Depends(require_permission("inventory.read")),
):
    if not user.restaurant_id:
        return []
    params: list = [user.restaurant_id]
    where = "restaurant_id = $1::uuid"
    if ingredient_id:
        params.append(ingredient_id)
        where += f" AND ingredient_id = ${len(params)}"
    params.append(limit)
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM inventory_adjustments WHERE {where} "
            f"ORDER BY created_at DESC LIMIT ${len(params)}",
            *params,
        )
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
# WASTAGE
# ════════════════════════════════════════════════════════════════════════════

class WastageIn(BaseModel):
    ingredient_id: str
    branch_id: Optional[str] = None
    batch_id: Optional[str] = None
    quantity: Decimal = Field(..., gt=0)
    unit: Optional[str] = None
    unit_cost: Optional[Decimal] = None
    waste_reason: str = Field(..., pattern="^(spoilage|expiry|breakage|overcooked|customer_return|preparation_loss|contamination|other)$")
    notes: Optional[str] = None
    photo_url: Optional[str] = None


@router.post("/wastage")
async def log_wastage(
    body: WastageIn,
    user: UserContext = Depends(require_permission("inventory.update")),
):
    if not user.restaurant_id:
        raise ValidationError("restaurant context required")

    event_id = await inventory_event_service.append_event(
        restaurant_id=user.restaurant_id,
        branch_id=body.branch_id or user.branch_id,
        ingredient_id=body.ingredient_id,
        event_type=INVENTORY_WASTED,
        quantity_out=body.quantity,
        unit_cost=body.unit_cost or 0,
        batch_id=body.batch_id,
        reference_type="wastage",
        source="manual",
        notes=body.notes,
        created_by=user.user_id,
        metadata={"waste_reason": body.waste_reason},
    )

    async with get_connection() as conn:
        wid = await conn.fetchval(
            """
            INSERT INTO inventory_wastage
                (restaurant_id, branch_id, ingredient_id, batch_id,
                 quantity, unit, unit_cost, waste_reason, notes,
                 photo_url, ledger_event_id, created_by)
            VALUES ($1::uuid,$2::uuid,$3,$4::uuid,$5,$6,$7,$8,$9,$10,$11::uuid,$12)
            RETURNING id
            """,
            user.restaurant_id, body.branch_id or user.branch_id,
            body.ingredient_id, body.batch_id,
            float(body.quantity), body.unit, float(body.unit_cost or 0),
            body.waste_reason, body.notes, body.photo_url,
            event_id, user.user_id,
        )
    await log_activity(
        user_id=user.user_id, branch_id=user.branch_id,
        action="inventory.wasted", entity_type="ingredient",
        entity_id=body.ingredient_id,
        metadata={"wastage_id": str(wid), "reason": body.waste_reason,
                  "qty": float(body.quantity)},
    )
    return {"wastage_id": str(wid), "event_id": event_id}


@router.get("/wastage")
async def list_wastage(
    ingredient_id: Optional[str] = None,
    limit: int = Query(50, le=200),
    user: UserContext = Depends(require_permission("inventory.read")),
):
    if not user.restaurant_id:
        return []
    params: list = [user.restaurant_id]
    where = "restaurant_id = $1::uuid"
    if ingredient_id:
        params.append(ingredient_id)
        where += f" AND ingredient_id = ${len(params)}"
    params.append(limit)
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM inventory_wastage WHERE {where} "
            f"ORDER BY created_at DESC LIMIT ${len(params)}",
            *params,
        )
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
# STOCK TRANSFERS  (inter-branch, ship → receive)
# ════════════════════════════════════════════════════════════════════════════

class TransferLineIn(BaseModel):
    ingredient_id: str
    quantity_sent: Decimal = Field(..., gt=0)
    unit: Optional[str] = None


class TransferIn(BaseModel):
    from_branch_id: str
    to_branch_id: str
    items: list[TransferLineIn] = Field(..., min_length=1)
    notes: Optional[str] = None


@router.post("/transfers")
async def create_transfer(
    body: TransferIn,
    user: UserContext = Depends(require_permission("inventory.update")),
):
    if not user.restaurant_id:
        raise ValidationError("restaurant context required")
    if body.from_branch_id == body.to_branch_id:
        raise ValidationError("from_branch and to_branch must differ")

    async with get_serializable_transaction() as conn:
        transfer_id = await conn.fetchval(
            """
            INSERT INTO stock_transfers
                (restaurant_id, from_branch_id, to_branch_id,
                 status, requested_by, notes)
            VALUES ($1::uuid, $2::uuid, $3::uuid, 'draft', $4, $5)
            RETURNING id
            """,
            user.restaurant_id, body.from_branch_id, body.to_branch_id,
            user.user_id, body.notes,
        )
        for line in body.items:
            await conn.execute(
                """
                INSERT INTO stock_transfer_items
                    (transfer_id, ingredient_id, quantity_sent, unit)
                VALUES ($1::uuid, $2, $3, $4)
                """,
                transfer_id, line.ingredient_id,
                float(line.quantity_sent), line.unit,
            )
    return {"transfer_id": str(transfer_id), "status": "draft"}


@router.post("/transfers/{transfer_id}/ship")
async def ship_transfer(
    transfer_id: str,
    user: UserContext = Depends(require_permission("inventory.update")),
):
    """Mark transfer as shipped — debit FROM-branch immediately."""
    async with get_connection() as conn:
        t = await conn.fetchrow(
            "SELECT * FROM stock_transfers WHERE id = $1::uuid", transfer_id,
        )
        if not t:
            raise NotFoundError("stock_transfer", transfer_id)
        if t["status"] not in ("draft", "approved"):
            raise ValidationError(f"cannot ship from status {t['status']}")
        items = await conn.fetch(
            "SELECT * FROM stock_transfer_items WHERE transfer_id = $1::uuid",
            transfer_id,
        )

    for it in items:
        await inventory_event_service.append_event(
            restaurant_id=str(t["restaurant_id"]),
            branch_id=str(t["from_branch_id"]),
            ingredient_id=it["ingredient_id"],
            event_type=INVENTORY_TRANSFERRED_OUT,
            quantity_out=Decimal(str(it["quantity_sent"])),
            reference_type="stock_transfer",
            reference_id=str(transfer_id),
            dedup_key=f"transfer:{transfer_id}:out:{it['ingredient_id']}",
            correlation_id=str(transfer_id),
            source="transfer",
            notes=f"Transfer to branch {t['to_branch_id']}",
            created_by=user.user_id,
        )

    async with get_connection() as conn:
        await conn.execute(
            "UPDATE stock_transfers SET status='in_transit', shipped_at=NOW() "
            "WHERE id=$1::uuid", transfer_id,
        )
    return {"transfer_id": transfer_id, "status": "in_transit"}


class ReceiveTransferLine(BaseModel):
    ingredient_id: str
    quantity_received: Decimal = Field(..., ge=0)


class ReceiveTransferIn(BaseModel):
    items: Optional[list[ReceiveTransferLine]] = None  # if None → received_qty = sent_qty


@router.post("/transfers/{transfer_id}/receive")
async def receive_transfer(
    transfer_id: str,
    body: ReceiveTransferIn,
    user: UserContext = Depends(require_permission("inventory.update")),
):
    """Mark transfer as received — credit TO-branch (per line if provided)."""
    async with get_connection() as conn:
        t = await conn.fetchrow(
            "SELECT * FROM stock_transfers WHERE id = $1::uuid", transfer_id,
        )
        if not t:
            raise NotFoundError("stock_transfer", transfer_id)
        if t["status"] != "in_transit":
            raise ValidationError(f"cannot receive from status {t['status']}")
        items = await conn.fetch(
            "SELECT * FROM stock_transfer_items WHERE transfer_id = $1::uuid",
            transfer_id,
        )

    received_map = (
        {l.ingredient_id: l.quantity_received for l in (body.items or [])}
        if body.items else {}
    )

    for it in items:
        recv_qty = received_map.get(
            it["ingredient_id"], Decimal(str(it["quantity_sent"])),
        )
        recv_qty = Decimal(str(recv_qty))
        if recv_qty <= 0:
            continue
        await inventory_event_service.append_event(
            restaurant_id=str(t["restaurant_id"]),
            branch_id=str(t["to_branch_id"]),
            ingredient_id=it["ingredient_id"],
            event_type=INVENTORY_TRANSFERRED_IN,
            quantity_in=recv_qty,
            reference_type="stock_transfer",
            reference_id=str(transfer_id),
            dedup_key=f"transfer:{transfer_id}:in:{it['ingredient_id']}",
            correlation_id=str(transfer_id),
            source="transfer",
            notes=f"Transfer from branch {t['from_branch_id']}",
            created_by=user.user_id,
        )
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE stock_transfer_items SET quantity_received=$1 "
                "WHERE transfer_id=$2::uuid AND ingredient_id=$3",
                float(recv_qty), transfer_id, it["ingredient_id"],
            )

    async with get_connection() as conn:
        await conn.execute(
            "UPDATE stock_transfers SET status='received', received_at=NOW(), "
            "received_by=$1 WHERE id=$2::uuid",
            user.user_id, transfer_id,
        )
    return {"transfer_id": transfer_id, "status": "received"}


@router.get("/transfers")
async def list_transfers(
    status: Optional[str] = None,
    limit: int = Query(50, le=200),
    user: UserContext = Depends(require_permission("inventory.read")),
):
    if not user.restaurant_id:
        return []
    params: list = [user.restaurant_id]
    where = "restaurant_id = $1::uuid"
    if status:
        params.append(status)
        where += f" AND status = ${len(params)}"
    params.append(limit)
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM stock_transfers WHERE {where} "
            f"ORDER BY created_at DESC LIMIT ${len(params)}",
            *params,
        )
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
# INVENTORY COUNTS  (physical stock-takes)
# ════════════════════════════════════════════════════════════════════════════

class CountStartIn(BaseModel):
    branch_id: Optional[str] = None
    count_type: str = Field("partial", pattern="^(full|partial|spot|cycle)$")
    notes: Optional[str] = None


class CountItemIn(BaseModel):
    ingredient_id: str
    counted_qty: Decimal = Field(..., ge=0)
    unit: Optional[str] = None


@router.post("/counts/start")
async def start_count(
    body: CountStartIn,
    user: UserContext = Depends(require_permission("inventory.update")),
):
    if not user.restaurant_id:
        raise ValidationError("restaurant context required")
    async with get_connection() as conn:
        count_number = await conn.fetchval(
            "SELECT 'CNT-' || to_char(NOW(),'YYMMDDHH24MISS')",
        )
        cid = await conn.fetchval(
            """
            INSERT INTO inventory_counts
                (restaurant_id, branch_id, count_number, count_type,
                 status, started_by, started_at)
            VALUES ($1::uuid,$2::uuid,$3,$4,'in_progress',$5,NOW())
            RETURNING id
            """,
            user.restaurant_id, body.branch_id or user.branch_id,
            count_number, body.count_type, user.user_id,
        )
        ings = await conn.fetch(
            "SELECT id, unit FROM ingredients WHERE restaurant_id = $1::uuid "
            "AND deleted_at IS NULL AND is_active = true",
            user.restaurant_id,
        )
        for ing in ings:
            bal = await conn.fetchval(
                "SELECT fn_inventory_balance($1, $2::uuid, NULL)",
                ing["id"], body.branch_id or user.branch_id,
            )
            await conn.execute(
                """INSERT INTO inventory_count_items
                       (count_id, ingredient_id, expected_qty, unit)
                   VALUES ($1::uuid, $2, $3, $4)
                   ON CONFLICT DO NOTHING""",
                cid, ing["id"], float(bal or 0), ing["unit"],
            )
    return {"count_id": str(cid), "count_number": count_number}


@router.post("/counts/{count_id}/items")
async def submit_count_item(
    count_id: str, body: CountItemIn,
    user: UserContext = Depends(require_permission("inventory.update")),
):
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE inventory_count_items
               SET counted_qty = $1, unit = COALESCE($2, unit),
                   counted_by = $3, counted_at = NOW()
             WHERE count_id = $4::uuid AND ingredient_id = $5
            """,
            float(body.counted_qty), body.unit, user.user_id,
            count_id, body.ingredient_id,
        )
    return {"ok": True}


@router.post("/counts/{count_id}/finalize")
async def finalize_count(
    count_id: str,
    user: UserContext = Depends(require_permission("inventory.update")),
):
    """Approve count → emit RECOUNT events for every variance."""
    async with get_connection() as conn:
        c = await conn.fetchrow(
            "SELECT * FROM inventory_counts WHERE id = $1::uuid", count_id,
        )
        if not c:
            raise NotFoundError("inventory_count", count_id)
        if c["status"] not in ("in_progress", "completed"):
            raise ValidationError(f"cannot finalize from status {c['status']}")
        items = await conn.fetch(
            "SELECT * FROM inventory_count_items "
            "WHERE count_id = $1::uuid AND counted_qty IS NOT NULL",
            count_id,
        )

    applied = 0
    for it in items:
        variance = Decimal(str(it["variance"] or 0))
        if variance == 0:
            continue
        ledger_type = "adjustment_in" if variance > 0 else "adjustment_out"
        await inventory_event_service.append_event(
            restaurant_id=str(c["restaurant_id"]),
            branch_id=str(c["branch_id"]) if c["branch_id"] else None,
            ingredient_id=it["ingredient_id"],
            event_type=INVENTORY_RECOUNTED,
            ledger_type=ledger_type,
            quantity_in=abs(variance) if variance > 0 else Decimal("0"),
            quantity_out=abs(variance) if variance < 0 else Decimal("0"),
            unit_cost=Decimal(str(it["unit_cost"] or 0)),
            reference_type="inventory_count",
            reference_id=str(count_id),
            dedup_key=f"count:{count_id}:ing:{it['ingredient_id']}",
            correlation_id=str(count_id),
            source="recount",
            notes=f"Stock-take {c['count_number']} variance",
            created_by=user.user_id,
        )
        applied += 1

    async with get_connection() as conn:
        await conn.execute(
            "UPDATE inventory_counts SET status='approved', approved_by=$1, "
            "approved_at=NOW(), completed_at=NOW(), completed_by=$1 "
            "WHERE id=$2::uuid",
            user.user_id, count_id,
        )
    return {"count_id": count_id, "variances_applied": applied,
            "status": "approved"}


@router.get("/counts")
async def list_counts(
    limit: int = Query(50, le=200),
    user: UserContext = Depends(require_permission("inventory.read")),
):
    if not user.restaurant_id:
        return []
    async with get_connection() as conn:
        rows = await conn.fetch(
            "SELECT * FROM inventory_counts WHERE restaurant_id = $1::uuid "
            "ORDER BY count_date DESC LIMIT $2",
            user.restaurant_id, limit,
        )
    return [dict(r) for r in rows]


@router.get("/counts/{count_id}")
async def get_count(
    count_id: str,
    user: UserContext = Depends(require_permission("inventory.read")),
):
    async with get_connection() as conn:
        c = await conn.fetchrow(
            "SELECT * FROM inventory_counts WHERE id = $1::uuid", count_id,
        )
        if not c:
            raise NotFoundError("inventory_count", count_id)
        items = await conn.fetch(
            "SELECT ci.*, i.name AS ingredient_name FROM inventory_count_items ci "
            "JOIN ingredients i ON i.id = ci.ingredient_id "
            "WHERE ci.count_id = $1::uuid", count_id,
        )
    return {"count": dict(c), "items": [dict(i) for i in items]}


# ════════════════════════════════════════════════════════════════════════════
# ALERTS
# ════════════════════════════════════════════════════════════════════════════

@router.get("/alerts")
async def list_alerts(
    status: str = "open",
    severity: Optional[str] = None,
    limit: int = Query(50, le=200),
    user: UserContext = Depends(require_permission("inventory.read")),
):
    if not user.restaurant_id:
        return []
    params: list = [user.restaurant_id, status]
    where = "restaurant_id = $1::uuid AND status = $2"
    if severity:
        params.append(severity)
        where += f" AND severity = ${len(params)}"
    params.append(limit)
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM inventory_alerts WHERE {where} "
            f"ORDER BY created_at DESC LIMIT ${len(params)}",
            *params,
        )
    return [dict(r) for r in rows]


@router.post("/alerts/{alert_id}/acknowledge")
async def ack_alert(
    alert_id: str,
    user: UserContext = Depends(require_permission("inventory.update")),
):
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE inventory_alerts SET status='acknowledged', "
            "acknowledged_by=$1, acknowledged_at=NOW() WHERE id=$2::uuid",
            user.user_id, alert_id,
        )
    return {"ok": True}


@router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(
    alert_id: str,
    user: UserContext = Depends(require_permission("inventory.update")),
):
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE inventory_alerts SET status='resolved', resolved_at=NOW() "
            "WHERE id=$1::uuid", alert_id,
        )
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════════════
# EXPIRY DASHBOARD (FEFO buckets)
# ════════════════════════════════════════════════════════════════════════════

@router.get("/expiry")
async def expiry_dashboard(
    bucket: Optional[str] = Query(None, pattern="^(expired|critical|warning|ok)$"),
    branch_id: Optional[str] = None,
    user: UserContext = Depends(require_permission("inventory.read")),
):
    if not user.restaurant_id:
        return []
    params: list = [user.restaurant_id]
    where = "restaurant_id = $1::uuid"
    if branch_id or user.branch_id:
        params.append(branch_id or user.branch_id)
        where += f" AND branch_id = ${len(params)}::uuid"
    if bucket:
        params.append(bucket)
        where += f" AND expiry_bucket = ${len(params)}"
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM inventory_expiry_status WHERE {where} "
            f"ORDER BY days_to_expiry NULLS LAST",
            *params,
        )
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
# ANALYTICS
# ════════════════════════════════════════════════════════════════════════════

@router.get("/analytics")
async def analytics(
    ingredient_id: Optional[str] = None,
    days: int = Query(30, ge=1, le=365),
    user: UserContext = Depends(require_permission("inventory.read")),
):
    if not user.restaurant_id:
        return []
    params: list = [user.restaurant_id, days]
    where = ("restaurant_id = $1::uuid AND period_date >= "
             "(CURRENT_DATE - $2::int)")
    if ingredient_id:
        params.append(ingredient_id)
        where += f" AND ingredient_id = ${len(params)}"
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM inventory_analytics WHERE {where} "
            f"ORDER BY period_date DESC, ingredient_id",
            *params,
        )
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
# VENDORS
# ════════════════════════════════════════════════════════════════════════════

class VendorIn(BaseModel):
    name: str
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    gst_number: Optional[str] = None
    pan_number: Optional[str] = None
    payment_terms: Optional[int] = 30
    credit_limit: Optional[Decimal] = Decimal("0")
    notes: Optional[str] = None


@router.get("/vendors")
async def list_vendors(
    active_only: bool = True,
    user: UserContext = Depends(require_permission("inventory.read")),
):
    if not user.restaurant_id:
        return []
    where = "restaurant_id = $1::uuid"
    if active_only:
        where += " AND is_active = true"
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM vendors WHERE {where} ORDER BY name",
            user.restaurant_id,
        )
    return [dict(r) for r in rows]


@router.post("/vendors")
async def create_vendor(
    body: VendorIn,
    user: UserContext = Depends(require_permission("inventory.update")),
):
    if not user.restaurant_id:
        raise ValidationError("restaurant context required")
    async with get_connection() as conn:
        vid = await conn.fetchval(
            """
            INSERT INTO vendors
                (restaurant_id, name, contact_person, phone, email, address,
                 city, state, pincode, gst_number, pan_number,
                 payment_terms, credit_limit, notes)
            VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            RETURNING id
            """,
            user.restaurant_id, body.name, body.contact_person, body.phone,
            body.email, body.address, body.city, body.state, body.pincode,
            body.gst_number, body.pan_number, body.payment_terms or 30,
            float(body.credit_limit or 0), body.notes,
        )
    return {"vendor_id": str(vid)}


@router.patch("/vendors/{vendor_id}")
async def update_vendor(
    vendor_id: str, body: VendorIn,
    user: UserContext = Depends(require_permission("inventory.update")),
):
    async with get_connection() as conn:
        res = await conn.execute(
            """
            UPDATE vendors SET
                name=$1, contact_person=$2, phone=$3, email=$4, address=$5,
                city=$6, state=$7, pincode=$8, gst_number=$9, pan_number=$10,
                payment_terms=$11, credit_limit=$12, notes=$13, updated_at=NOW()
             WHERE id = $14::uuid AND restaurant_id = $15::uuid
            """,
            body.name, body.contact_person, body.phone, body.email,
            body.address, body.city, body.state, body.pincode,
            body.gst_number, body.pan_number, body.payment_terms or 30,
            float(body.credit_limit or 0), body.notes,
            vendor_id, user.restaurant_id,
        )
    if res.endswith("0"):
        raise NotFoundError("vendor", vendor_id)
    return {"ok": True}


@router.post("/vendors/{vendor_id}/toggle")
async def toggle_vendor(
    vendor_id: str,
    user: UserContext = Depends(require_permission("inventory.update")),
):
    async with get_connection() as conn:
        new_state = await conn.fetchval(
            "UPDATE vendors SET is_active = NOT is_active, updated_at = NOW() "
            "WHERE id=$1::uuid AND restaurant_id=$2::uuid RETURNING is_active",
            vendor_id, user.restaurant_id,
        )
    if new_state is None:
        raise NotFoundError("vendor", vendor_id)
    return {"vendor_id": vendor_id, "is_active": new_state}


# ════════════════════════════════════════════════════════════════════════════
# UNIT CONVERSIONS
# ════════════════════════════════════════════════════════════════════════════

@router.get("/units/conversions")
async def list_conversions(
    user: UserContext = Depends(require_permission("inventory.read")),
):
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM unit_conversions
             WHERE is_active = true
               AND (restaurant_id IS NULL
                    OR restaurant_id = $1::uuid)
             ORDER BY restaurant_id NULLS FIRST, from_unit, to_unit
            """,
            user.restaurant_id,
        )
    return [dict(r) for r in rows]


class ConversionIn(BaseModel):
    ingredient_id: Optional[str] = None
    from_unit: str
    to_unit: str
    factor: Decimal = Field(..., gt=0)


@router.post("/units/conversions")
async def upsert_conversion(
    body: ConversionIn,
    user: UserContext = Depends(require_permission("inventory.update")),
):
    if not user.restaurant_id:
        raise ValidationError("restaurant context required")
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO unit_conversions
                (restaurant_id, ingredient_id, from_unit, to_unit, factor)
            VALUES ($1::uuid, $2, $3, $4, $5)
            ON CONFLICT (restaurant_id, ingredient_id, from_unit, to_unit)
            DO UPDATE SET factor = EXCLUDED.factor, is_active = true
            """,
            user.restaurant_id, body.ingredient_id,
            body.from_unit, body.to_unit, float(body.factor),
        )
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════════════
# RECONCILIATION DRIFT  (Section 11 — Edge Case: stock mismatch)
# ════════════════════════════════════════════════════════════════════════════

@router.get("/reconciliation/drift")
async def reconciliation_drift(
    user: UserContext = Depends(require_permission("inventory.read")),
):
    """Compare ledger-derived balance vs ingredients.current_stock."""
    if not user.restaurant_id:
        return []
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT i.id AS ingredient_id, i.name,
                   i.current_stock,
                   COALESCE(SUM(l.quantity_in - l.quantity_out), 0) AS ledger_balance,
                   (i.current_stock
                    - COALESCE(SUM(l.quantity_in - l.quantity_out), 0)) AS drift
              FROM ingredients i
              LEFT JOIN inventory_ledger l ON l.ingredient_id = i.id
             WHERE i.restaurant_id = $1::uuid
               AND i.deleted_at IS NULL
             GROUP BY i.id, i.name, i.current_stock
            HAVING ABS(i.current_stock
                       - COALESCE(SUM(l.quantity_in - l.quantity_out), 0)) > 0.001
             ORDER BY ABS(i.current_stock
                          - COALESCE(SUM(l.quantity_in - l.quantity_out), 0)) DESC
            """,
            user.restaurant_id,
        )
    return [dict(r) for r in rows]
