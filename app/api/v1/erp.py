"""ERP endpoints — Chart of Accounts, Journals, Recipes, Inventory Ledger,
Vendors, GRN, Vendor Payments, Shifts, Stock Transfers, Tax Rates, GST Reports,
Profitability, Daily P&L."""

from datetime import date, timedelta
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel

from app.core.auth import UserContext, require_role
from app.core.database import get_connection, get_transaction
from app.core.logging import get_logger

router = APIRouter(prefix="/erp", tags=["ERP"])
logger = get_logger(__name__)


def _uid(user: UserContext) -> str:
    return user.owner_id if user.is_branch_user else user.user_id


def _rid(user: UserContext) -> str:
    return user.restaurant_id


def _bid(user: UserContext, branch_id: Optional[str] = None) -> Optional[str]:
    return branch_id or user.branch_id


# ═══════════════════════════════════════════════════════════════════════════
# Pydantic Models
# ═══════════════════════════════════════════════════════════════════════════

class AccountCreate(BaseModel):
    account_code: str
    name: str
    account_type: str
    parent_id: Optional[str] = None
    description: Optional[str] = None


class JournalLineIn(BaseModel):
    account_code: str
    debit: float = 0
    credit: float = 0
    description: Optional[str] = None


class JournalCreate(BaseModel):
    entry_date: Optional[date] = None
    reference_type: str = "adjustment"
    description: Optional[str] = None
    lines: List[JournalLineIn]


class RecipeIngredientIn(BaseModel):
    ingredient_id: str
    quantity_required: float
    unit: Optional[str] = None
    waste_percent: float = 0
    notes: Optional[str] = None


class RecipeCreate(BaseModel):
    item_id: int
    name: Optional[str] = None
    yield_quantity: float = 1
    yield_unit: str = "portion"
    notes: Optional[str] = None
    ingredients: List[RecipeIngredientIn] = []


class RecipeUpdate(BaseModel):
    name: Optional[str] = None
    yield_quantity: Optional[float] = None
    yield_unit: Optional[str] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None
    ingredients: Optional[List[RecipeIngredientIn]] = None


class StockAdjust(BaseModel):
    ingredient_id: str
    branch_id: Optional[str] = None
    transaction_type: str  # adjustment_in, adjustment_out, wastage
    quantity: float
    unit_cost: float = 0
    notes: Optional[str] = None


class VendorCreate(BaseModel):
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
    bank_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc: Optional[str] = None
    payment_terms: int = 30
    credit_limit: float = 0
    notes: Optional[str] = None


class VendorUpdate(BaseModel):
    name: Optional[str] = None
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    gst_number: Optional[str] = None
    pan_number: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc: Optional[str] = None
    payment_terms: Optional[int] = None
    credit_limit: Optional[float] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None


class GRNItemIn(BaseModel):
    ingredient_id: str
    ordered_quantity: float = 0
    received_quantity: float
    rejected_quantity: float = 0
    unit: Optional[str] = None
    unit_cost: float = 0
    batch_number: Optional[str] = None
    expiry_date: Optional[date] = None
    notes: Optional[str] = None


class GRNCreate(BaseModel):
    purchase_order_id: Optional[int] = None
    vendor_id: Optional[str] = None
    branch_id: Optional[str] = None
    received_date: Optional[date] = None
    notes: Optional[str] = None
    items: List[GRNItemIn] = []


class VendorPaymentCreate(BaseModel):
    vendor_id: str
    amount: float
    payment_method: str
    payment_date: Optional[date] = None
    reference_number: Optional[str] = None
    purchase_order_id: Optional[int] = None
    grn_id: Optional[str] = None
    notes: Optional[str] = None


class ShiftOpen(BaseModel):
    drawer_id: Optional[str] = None
    opening_cash: float = 0


class ShiftClose(BaseModel):
    closing_cash: float
    notes: Optional[str] = None


class DrawerCreate(BaseModel):
    name: str
    branch_id: Optional[str] = None


class TransferItemIn(BaseModel):
    ingredient_id: str
    quantity_sent: float
    unit: Optional[str] = None
    notes: Optional[str] = None


class TransferCreate(BaseModel):
    from_branch_id: str
    to_branch_id: str
    notes: Optional[str] = None
    items: List[TransferItemIn] = []


class TransferReceiveItem(BaseModel):
    ingredient_id: str
    quantity_received: float
    notes: Optional[str] = None


class TransferReceive(BaseModel):
    items: List[TransferReceiveItem] = []


class TaxRateCreate(BaseModel):
    name: str
    hsn_code: Optional[str] = None
    rate_percentage: float
    cgst_percentage: float = 0
    sgst_percentage: float = 0
    igst_percentage: float = 0
    is_inclusive: bool = False
    applicable_on: str = "all"


class TaxAssign(BaseModel):
    item_id: int
    tax_rate_id: str


class GSTReportGenerate(BaseModel):
    report_type: str  # GSTR1, GSTR3B, tax_liability
    period_start: date
    period_end: date


# ═══════════════════════════════════════════════════════════════════════════
# 1. CHART OF ACCOUNTS
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/accounts")
async def list_accounts(
    account_type: Optional[str] = Query(None),
    is_active: bool = Query(True),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    clauses = ["restaurant_id = $1", "is_active = $2"]
    params: list = [rid, is_active]
    if account_type:
        params.append(account_type)
        clauses.append(f"account_type = ${len(params)}")
    sql = f"""
        SELECT id, account_code, name, account_type, parent_id,
               description, is_system, is_active, created_at
          FROM chart_of_accounts
         WHERE {' AND '.join(clauses)}
         ORDER BY account_code
    """
    async with get_connection() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


@router.post("/accounts", status_code=201)
async def create_account(
    body: AccountCreate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """INSERT INTO chart_of_accounts
                   (restaurant_id, account_code, name, account_type, parent_id, description)
               VALUES ($1, $2, $3, $4, $5::uuid, $6)
               RETURNING *""",
            rid, body.account_code, body.name, body.account_type,
            body.parent_id, body.description,
        )
    return dict(row)


@router.get("/accounts/balances")
async def account_balances(
    as_of_date: Optional[date] = Query(None),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    if as_of_date:
        sql = """
            SELECT coa.account_code, coa.name, coa.account_type,
                   COALESCE(SUM(jl.debit), 0)  AS total_debit,
                   COALESCE(SUM(jl.credit), 0) AS total_credit,
                   CASE WHEN coa.account_type IN ('asset','expense')
                        THEN COALESCE(SUM(jl.debit),0) - COALESCE(SUM(jl.credit),0)
                        ELSE COALESCE(SUM(jl.credit),0) - COALESCE(SUM(jl.debit),0)
                   END AS balance
              FROM chart_of_accounts coa
              LEFT JOIN journal_lines jl ON jl.account_id = coa.id
              LEFT JOIN journal_entries je ON je.id = jl.journal_entry_id
                   AND je.is_reversed = false AND je.entry_date <= $2
             WHERE coa.restaurant_id = $1 AND coa.is_active = true
             GROUP BY coa.account_code, coa.name, coa.account_type
             ORDER BY coa.account_code
        """
        async with get_connection() as conn:
            rows = await conn.fetch(sql, rid, as_of_date)
    else:
        sql = """
            SELECT account_code, name, account_type,
                   total_debit, total_credit, balance
              FROM v_account_balances
             WHERE restaurant_id = $1
             ORDER BY account_code
        """
        async with get_connection() as conn:
            rows = await conn.fetch(sql, rid)
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# 2. JOURNAL ENTRIES
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/journals")
async def list_journals(
    reference_type: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    branch_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    bid = _bid(user, branch_id)
    if not start_date:
        start_date = date.today() - timedelta(days=30)
    if not end_date:
        end_date = date.today()

    clauses = ["je.restaurant_id = $1", "je.entry_date >= $2", "je.entry_date <= $3"]
    params: list = [rid, start_date, end_date]
    if reference_type:
        params.append(reference_type)
        clauses.append(f"je.reference_type = ${len(params)}")
    if bid:
        params.append(bid)
        clauses.append(f"je.branch_id = ${len(params)}::uuid")
    params.extend([limit, offset])
    sql = f"""
        SELECT je.id, je.entry_date, je.reference_type, je.reference_id,
               je.description, je.is_reversed, je.reversed_by,
               je.created_by, je.created_at
          FROM journal_entries je
         WHERE {' AND '.join(clauses)}
         ORDER BY je.entry_date DESC, je.created_at DESC
         LIMIT ${len(params)-1} OFFSET ${len(params)}
    """
    async with get_connection() as conn:
        entries = await conn.fetch(sql, *params)
        result = []
        for e in entries:
            entry = dict(e)
            lines = await conn.fetch(
                """SELECT coa.account_code, coa.name AS account_name,
                          jl.debit, jl.credit, jl.description
                     FROM journal_lines jl
                     JOIN chart_of_accounts coa ON coa.id = jl.account_id
                    WHERE jl.journal_entry_id = $1
                    ORDER BY jl.debit DESC, jl.credit DESC""",
                e["id"],
            )
            entry["lines"] = [dict(l) for l in lines]
            entry["total_debit"] = float(sum(l["debit"] for l in lines))
            entry["total_credit"] = float(sum(l["credit"] for l in lines))
            result.append(entry)
    return result


@router.post("/journals", status_code=201)
async def create_journal(
    body: JournalCreate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    uid = _uid(user)
    bid = user.branch_id
    entry_date = body.entry_date or date.today()

    total_dr = sum(l.debit for l in body.lines)
    total_cr = sum(l.credit for l in body.lines)
    if abs(total_dr - total_cr) > 0.01:
        raise HTTPException(400, f"Unbalanced: debit={total_dr}, credit={total_cr}")
    if len(body.lines) < 2:
        raise HTTPException(400, "Minimum 2 journal lines required")

    async with get_transaction() as conn:
        entry = await conn.fetchrow(
            """INSERT INTO journal_entries
                   (restaurant_id, branch_id, entry_date, reference_type, description, created_by)
               VALUES ($1, $2::uuid, $3, $4, $5, $6)
               RETURNING *""",
            rid, bid, entry_date, body.reference_type, body.description, uid,
        )
        for ln in body.lines:
            acct = await conn.fetchrow(
                "SELECT id FROM chart_of_accounts WHERE restaurant_id=$1 AND account_code=$2",
                rid, ln.account_code,
            )
            if not acct:
                raise HTTPException(400, f"Account code {ln.account_code} not found")
            await conn.execute(
                """INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit, description)
                   VALUES ($1, $2, $3, $4, $5)""",
                entry["id"], acct["id"], ln.debit, ln.credit, ln.description,
            )
        lines = await conn.fetch(
            """SELECT coa.account_code, coa.name AS account_name, jl.debit, jl.credit, jl.description
                 FROM journal_lines jl
                 JOIN chart_of_accounts coa ON coa.id = jl.account_id
                WHERE jl.journal_entry_id = $1""",
            entry["id"],
        )
    result = dict(entry)
    result["lines"] = [dict(l) for l in lines]
    result["total_debit"] = float(total_dr)
    result["total_credit"] = float(total_cr)
    return result


@router.post("/journals/{journal_id}/reverse", status_code=201)
async def reverse_journal(
    journal_id: UUID,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    uid = _uid(user)
    async with get_transaction() as conn:
        orig = await conn.fetchrow(
            "SELECT * FROM journal_entries WHERE id=$1 AND restaurant_id=$2",
            journal_id, rid,
        )
        if not orig:
            raise HTTPException(404, "Journal entry not found")
        if orig["is_reversed"]:
            raise HTTPException(400, "Already reversed")

        reversal = await conn.fetchrow(
            """INSERT INTO journal_entries
                   (restaurant_id, branch_id, entry_date, reference_type, reference_id,
                    description, created_by)
               VALUES ($1, $2, CURRENT_DATE, $3, $4, $5, $6)
               RETURNING *""",
            rid, orig["branch_id"], orig["reference_type"], orig["reference_id"],
            f"Reversal of: {orig['description'] or str(journal_id)}", uid,
        )
        orig_lines = await conn.fetch(
            "SELECT * FROM journal_lines WHERE journal_entry_id=$1", journal_id,
        )
        for ol in orig_lines:
            await conn.execute(
                """INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit, description)
                   VALUES ($1, $2, $3, $4, $5)""",
                reversal["id"], ol["account_id"], ol["credit"], ol["debit"],
                f"Reversal: {ol['description'] or ''}",
            )
        await conn.execute(
            "UPDATE journal_entries SET is_reversed=true, reversed_by=$1 WHERE id=$2",
            reversal["id"], journal_id,
        )
    return dict(reversal)


# ═══════════════════════════════════════════════════════════════════════════
# 3. RECIPES
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/recipes")
async def list_recipes(
    item_id: Optional[int] = Query(None),
    is_active: bool = Query(True),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    clauses = ["r.restaurant_id = $1", "r.is_active = $2"]
    params: list = [rid, is_active]
    if item_id:
        params.append(item_id)
        clauses.append(f"r.item_id = ${len(params)}")
    sql = f"""
        SELECT r.id, r.item_id, i."Item_Name" AS item_name,
               r.name, r.yield_quantity, r.yield_unit,
               r.is_active, r.notes, r.created_at
          FROM recipes r
          LEFT JOIN items i ON i."Item_ID" = r.item_id
         WHERE {' AND '.join(clauses)}
         ORDER BY r.created_at DESC
    """
    async with get_connection() as conn:
        recipes = await conn.fetch(sql, *params)
        result = []
        for rec in recipes:
            rd = dict(rec)
            ings = await conn.fetch(
                """SELECT ri.id, ri.ingredient_id,
                          ig.name AS ingredient_name,
                          ri.quantity_required, ri.unit, ri.waste_percent, ri.notes,
                          COALESCE(ig.cost_per_unit, 0) AS unit_cost,
                          ROUND(ri.quantity_required * (1 + ri.waste_percent/100)
                                * COALESCE(ig.cost_per_unit, 0), 2) AS line_cost
                     FROM recipe_ingredients ri
                     JOIN ingredients ig ON ig.id = ri.ingredient_id
                    WHERE ri.recipe_id = $1""",
                rec["id"],
            )
            rd["ingredients"] = [dict(ig) for ig in ings]
            rd["total_cost"] = float(sum(ig["line_cost"] for ig in ings))
            result.append(rd)
    return result


@router.post("/recipes", status_code=201)
async def create_recipe(
    body: RecipeCreate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    uid = _uid(user)
    async with get_transaction() as conn:
        recipe = await conn.fetchrow(
            """INSERT INTO recipes
                   (restaurant_id, item_id, name, yield_quantity, yield_unit, notes, created_by)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               RETURNING *""",
            rid, body.item_id, body.name, body.yield_quantity,
            body.yield_unit, body.notes, uid,
        )
        for ing in body.ingredients:
            await conn.execute(
                """INSERT INTO recipe_ingredients
                       (recipe_id, ingredient_id, quantity_required, unit, waste_percent, notes)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                recipe["id"], ing.ingredient_id, ing.quantity_required,
                ing.unit, ing.waste_percent, ing.notes,
            )
    return dict(recipe)


@router.patch("/recipes/{recipe_id}")
async def update_recipe(
    recipe_id: UUID,
    body: RecipeUpdate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    async with get_transaction() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM recipes WHERE id=$1 AND restaurant_id=$2", recipe_id, rid,
        )
        if not existing:
            raise HTTPException(404, "Recipe not found")
        updates = {}
        if body.name is not None:
            updates["name"] = body.name
        if body.yield_quantity is not None:
            updates["yield_quantity"] = body.yield_quantity
        if body.yield_unit is not None:
            updates["yield_unit"] = body.yield_unit
        if body.notes is not None:
            updates["notes"] = body.notes
        if body.is_active is not None:
            updates["is_active"] = body.is_active
        if updates:
            set_clause = ", ".join(f"{k}=${i+2}" for i, k in enumerate(updates))
            await conn.execute(
                f"UPDATE recipes SET {set_clause}, updated_at=NOW() WHERE id=$1",
                recipe_id, *updates.values(),
            )
        if body.ingredients is not None:
            await conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id=$1", recipe_id)
            for ing in body.ingredients:
                await conn.execute(
                    """INSERT INTO recipe_ingredients
                           (recipe_id, ingredient_id, quantity_required, unit, waste_percent, notes)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    recipe_id, ing.ingredient_id, ing.quantity_required,
                    ing.unit, ing.waste_percent, ing.notes,
                )
        row = await conn.fetchrow("SELECT * FROM recipes WHERE id=$1", recipe_id)
    return dict(row)


# ═══════════════════════════════════════════════════════════════════════════
# 4. INVENTORY LEDGER
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/inventory-ledger/summary")
async def inventory_summary(
    branch_id: Optional[str] = Query(None),
    low_only: bool = Query(False),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    bid = _bid(user, branch_id)
    sql = """
        SELECT v.ingredient_id, v.ingredient_name, v.unit,
               v.current_stock, v.weighted_avg_cost,
               ROUND(v.current_stock * v.weighted_avg_cost, 2) AS stock_value,
               v.last_movement_at,
               0 AS reorder_point,
               false AS is_low
          FROM v_ingredient_stock_ledger v
         WHERE v.restaurant_id = $1
    """
    params: list = [rid]
    if bid:
        params.append(bid)
        sql += f" AND v.branch_id = ${len(params)}::uuid"
    sql += " ORDER BY v.ingredient_name"
    async with get_connection() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


@router.get("/inventory-ledger/history")
async def inventory_history(
    ingredient_id: str = Query(...),
    branch_id: Optional[str] = Query(None),
    transaction_type: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    bid = _bid(user, branch_id)
    clauses = ["il.restaurant_id = $1", "il.ingredient_id = $2"]
    params: list = [rid, ingredient_id]
    if bid:
        params.append(bid)
        clauses.append(f"il.branch_id = ${len(params)}::uuid")
    if transaction_type:
        params.append(transaction_type)
        clauses.append(f"il.transaction_type = ${len(params)}")
    params.extend([limit, offset])
    sql = f"""
        SELECT il.id, il.transaction_type, il.quantity_in, il.quantity_out,
               il.unit_cost, il.reference_type, il.reference_id,
               il.notes, il.created_by, il.created_at
          FROM inventory_ledger il
         WHERE {' AND '.join(clauses)}
         ORDER BY il.created_at DESC
         LIMIT ${len(params)-1} OFFSET ${len(params)}
    """
    async with get_connection() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


@router.post("/inventory-ledger/adjust", status_code=201)
async def adjust_stock(
    body: StockAdjust,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    uid = _uid(user)
    bid = _bid(user, body.branch_id)
    allowed = ("adjustment_in", "adjustment_out", "wastage")
    if body.transaction_type not in allowed:
        raise HTTPException(400, f"transaction_type must be one of {allowed}")
    q_in = body.quantity if body.transaction_type == "adjustment_in" else 0
    q_out = body.quantity if body.transaction_type != "adjustment_in" else 0
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """INSERT INTO inventory_ledger
                   (restaurant_id, branch_id, ingredient_id, transaction_type,
                    quantity_in, quantity_out, unit_cost, reference_type, notes, created_by)
               VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, 'manual', $8, $9)
               RETURNING *""",
            rid, bid, body.ingredient_id, body.transaction_type,
            q_in, q_out, body.unit_cost, body.notes, uid,
        )
    return dict(row)


# ═══════════════════════════════════════════════════════════════════════════
# 5. VENDORS
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/vendors")
async def list_vendors(
    is_active: bool = Query(True),
    search: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    clauses = ["v.restaurant_id = $1", "v.is_active = $2"]
    params: list = [rid, is_active]
    if search:
        params.append(f"%{search}%")
        clauses.append(
            f"(v.name ILIKE ${len(params)} OR v.phone ILIKE ${len(params)} OR v.gst_number ILIKE ${len(params)})"
        )
    params.extend([limit, offset])
    sql = f"""
        SELECT v.id, v.name, v.contact_person, v.phone, v.email,
               v.gst_number, v.payment_terms, v.credit_limit, v.is_active,
               COALESCE(vb.total_purchased, 0) AS total_purchased,
               COALESCE(vb.total_paid, 0) AS total_paid,
               COALESCE(vb.balance_due, 0) AS balance_due
          FROM vendors v
          LEFT JOIN v_vendor_balances vb ON vb.vendor_id = v.id
         WHERE {' AND '.join(clauses)}
         ORDER BY v.name
         LIMIT ${len(params)-1} OFFSET ${len(params)}
    """
    async with get_connection() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


@router.post("/vendors", status_code=201)
async def create_vendor(
    body: VendorCreate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """INSERT INTO vendors
                   (restaurant_id, name, contact_person, phone, email,
                    address, city, state, pincode,
                    gst_number, pan_number,
                    bank_name, bank_account_number, bank_ifsc,
                    payment_terms, credit_limit, notes)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
               RETURNING *""",
            rid, body.name, body.contact_person, body.phone, body.email,
            body.address, body.city, body.state, body.pincode,
            body.gst_number, body.pan_number,
            body.bank_name, body.bank_account_number, body.bank_ifsc,
            body.payment_terms, body.credit_limit, body.notes,
        )
    return dict(row)


@router.get("/vendors/{vendor_id}")
async def get_vendor(
    vendor_id: UUID,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """SELECT v.*,
                      COALESCE(vb.total_purchased, 0) AS total_purchased,
                      COALESCE(vb.total_paid, 0) AS total_paid,
                      COALESCE(vb.balance_due, 0) AS balance_due
                 FROM vendors v
                 LEFT JOIN v_vendor_balances vb ON vb.vendor_id = v.id
                WHERE v.id = $1 AND v.restaurant_id = $2""",
            vendor_id, rid,
        )
    if not row:
        raise HTTPException(404, "Vendor not found")
    return dict(row)


@router.patch("/vendors/{vendor_id}")
async def update_vendor(
    vendor_id: UUID,
    body: VendorUpdate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    data = body.dict(exclude_unset=True)
    if not data:
        raise HTTPException(400, "No fields to update")
    set_parts = []
    params: list = [vendor_id, rid]
    for k, v in data.items():
        params.append(v)
        set_parts.append(f"{k} = ${len(params)}")
    sql = f"""UPDATE vendors SET {', '.join(set_parts)}, updated_at=NOW()
              WHERE id=$1 AND restaurant_id=$2 RETURNING *"""
    async with get_connection() as conn:
        row = await conn.fetchrow(sql, *params)
    if not row:
        raise HTTPException(404, "Vendor not found")
    return dict(row)


# ═══════════════════════════════════════════════════════════════════════════
# 6. GOODS RECEIPT NOTES (GRN)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/grn")
async def list_grn(
    status: Optional[str] = Query(None),
    vendor_id: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    clauses = ["g.restaurant_id = $1"]
    params: list = [rid]
    if status:
        params.append(status)
        clauses.append(f"g.status = ${len(params)}")
    if vendor_id:
        params.append(vendor_id)
        clauses.append(f"g.vendor_id = ${len(params)}::uuid")
    if start_date:
        params.append(start_date)
        clauses.append(f"g.received_date >= ${len(params)}")
    if end_date:
        params.append(end_date)
        clauses.append(f"g.received_date <= ${len(params)}")
    params.extend([limit, offset])
    sql = f"""
        SELECT g.id, g.grn_number, v.name AS vendor_name,
               g.purchase_order_id, g.received_date,
               g.total_amount, g.status, g.received_by, g.created_at,
               (SELECT COUNT(*) FROM grn_items gi WHERE gi.grn_id = g.id) AS items_count
          FROM goods_receipt_notes g
          LEFT JOIN vendors v ON v.id = g.vendor_id
         WHERE {' AND '.join(clauses)}
         ORDER BY g.created_at DESC
         LIMIT ${len(params)-1} OFFSET ${len(params)}
    """
    async with get_connection() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


@router.post("/grn", status_code=201)
async def create_grn(
    body: GRNCreate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    uid = _uid(user)
    bid = _bid(user, body.branch_id)
    received_date = body.received_date or date.today()
    async with get_transaction() as conn:
        total = sum(i.received_quantity * i.unit_cost for i in body.items)
        grn = await conn.fetchrow(
            """INSERT INTO goods_receipt_notes
                   (restaurant_id, branch_id, purchase_order_id, vendor_id,
                    received_date, total_amount, notes, received_by)
               VALUES ($1, $2::uuid, $3, $4::uuid, $5, $6, $7, $8)
               RETURNING *""",
            rid, bid, body.purchase_order_id, body.vendor_id,
            received_date, total, body.notes, uid,
        )
        for item in body.items:
            line_total = item.received_quantity * item.unit_cost
            await conn.execute(
                """INSERT INTO grn_items
                       (grn_id, ingredient_id, ordered_quantity, received_quantity,
                        rejected_quantity, unit, unit_cost, line_total,
                        batch_number, expiry_date, notes)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
                grn["id"], item.ingredient_id, item.ordered_quantity,
                item.received_quantity, item.rejected_quantity,
                item.unit, item.unit_cost, line_total,
                item.batch_number, item.expiry_date, item.notes,
            )
    return dict(grn)


@router.patch("/grn/{grn_id}/verify")
async def verify_grn(
    grn_id: UUID,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    uid = _uid(user)
    async with get_transaction() as conn:
        grn = await conn.fetchrow(
            "SELECT * FROM goods_receipt_notes WHERE id=$1 AND restaurant_id=$2",
            grn_id, rid,
        )
        if not grn:
            raise HTTPException(404, "GRN not found")
        if grn["status"] != "draft":
            raise HTTPException(400, f"Cannot verify GRN in status: {grn['status']}")

        await conn.execute(
            "UPDATE goods_receipt_notes SET status='verified', verified_by=$1, updated_at=NOW() WHERE id=$2",
            uid, grn_id,
        )
        items = await conn.fetch("SELECT * FROM grn_items WHERE grn_id=$1", grn_id)
        for item in items:
            await conn.execute(
                """INSERT INTO inventory_ledger
                       (restaurant_id, branch_id, ingredient_id, transaction_type,
                        quantity_in, quantity_out, unit_cost, reference_type, reference_id, created_by)
                   VALUES ($1, $2, $3, 'purchase', $4, 0, $5, 'grn', $6, $7)""",
                rid, grn["branch_id"], item["ingredient_id"],
                item["received_quantity"], item["unit_cost"],
                grn["grn_number"], uid,
            )
    return {"status": "verified", "grn_id": str(grn_id)}


# ═══════════════════════════════════════════════════════════════════════════
# 7. VENDOR PAYMENTS
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/vendor-payments")
async def list_vendor_payments(
    vendor_id: Optional[str] = Query(None),
    payment_method: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    clauses = ["vp.restaurant_id = $1"]
    params: list = [rid]
    if vendor_id:
        params.append(vendor_id)
        clauses.append(f"vp.vendor_id = ${len(params)}::uuid")
    if payment_method:
        params.append(payment_method)
        clauses.append(f"vp.payment_method = ${len(params)}")
    if start_date:
        params.append(start_date)
        clauses.append(f"vp.payment_date >= ${len(params)}")
    if end_date:
        params.append(end_date)
        clauses.append(f"vp.payment_date <= ${len(params)}")
    params.extend([limit, offset])
    sql = f"""
        SELECT vp.id, v.name AS vendor_name, vp.amount,
               vp.payment_method, vp.payment_date, vp.reference_number,
               g.grn_number, vp.notes, vp.created_by, vp.created_at
          FROM vendor_payments vp
          JOIN vendors v ON v.id = vp.vendor_id
          LEFT JOIN goods_receipt_notes g ON g.id = vp.grn_id
         WHERE {' AND '.join(clauses)}
         ORDER BY vp.payment_date DESC
         LIMIT ${len(params)-1} OFFSET ${len(params)}
    """
    async with get_connection() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


@router.post("/vendor-payments", status_code=201)
async def create_vendor_payment(
    body: VendorPaymentCreate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    uid = _uid(user)
    bid = user.branch_id
    payment_date = body.payment_date or date.today()
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """INSERT INTO vendor_payments
                   (restaurant_id, branch_id, vendor_id, amount, payment_method,
                    payment_date, reference_number, purchase_order_id, grn_id, notes, created_by)
               VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9::uuid, $10, $11)
               RETURNING *""",
            rid, bid, body.vendor_id, body.amount, body.payment_method,
            payment_date, body.reference_number, body.purchase_order_id,
            body.grn_id, body.notes, uid,
        )
    return dict(row)


# ═══════════════════════════════════════════════════════════════════════════
# 8. CASH DRAWERS & SHIFTS
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/drawers")
async def list_drawers(
    user: UserContext = Depends(require_role("owner", "manager", "cashier")),
):
    rid = _rid(user)
    bid = user.branch_id
    async with get_connection() as conn:
        rows = await conn.fetch(
            """SELECT * FROM cash_drawers
               WHERE restaurant_id=$1 AND ($2::uuid IS NULL OR branch_id=$2::uuid)
               ORDER BY name""",
            rid, bid,
        )
    return [dict(r) for r in rows]


@router.post("/drawers", status_code=201)
async def create_drawer(
    body: DrawerCreate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    bid = body.branch_id or user.branch_id
    if not bid:
        raise HTTPException(400, "branch_id is required")
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """INSERT INTO cash_drawers (restaurant_id, branch_id, name)
               VALUES ($1, $2::uuid, $3) RETURNING *""",
            rid, bid, body.name,
        )
    return dict(row)


@router.post("/shifts/open", status_code=201)
async def open_shift(
    body: ShiftOpen,
    user: UserContext = Depends(require_role("owner", "manager", "cashier")),
):
    rid = _rid(user)
    uid = _uid(user)
    bid = user.branch_id
    if not bid:
        raise HTTPException(400, "Branch context required")
    async with get_connection() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM shifts WHERE user_id=$1 AND status='open'", uid,
        )
        if existing:
            raise HTTPException(400, "You already have an open shift")
        row = await conn.fetchrow(
            """INSERT INTO shifts
                   (restaurant_id, branch_id, drawer_id, user_id, opening_cash)
               VALUES ($1, $2::uuid, $3::uuid, $4, $5)
               RETURNING *""",
            rid, bid, body.drawer_id, uid, body.opening_cash,
        )
    return dict(row)


@router.get("/shifts/current")
async def current_shift(
    user: UserContext = Depends(require_role("owner", "manager", "cashier")),
):
    uid = _uid(user)
    async with get_connection() as conn:
        shift = await conn.fetchrow(
            """SELECT s.*, cd.name AS drawer_name
                 FROM shifts s
                 LEFT JOIN cash_drawers cd ON cd.id = s.drawer_id
                WHERE s.user_id = $1 AND s.status = 'open'""",
            uid,
        )
        if not shift:
            raise HTTPException(404, "No open shift")
        summary = await conn.fetchrow(
            """SELECT
                   COALESCE(SUM(CASE WHEN transaction_type='sale' AND payment_method='cash' THEN amount ELSE 0 END), 0) AS total_sales_cash,
                   COALESCE(SUM(CASE WHEN transaction_type='sale' AND payment_method!='cash' THEN amount ELSE 0 END), 0) AS total_sales_digital,
                   COALESCE(SUM(CASE WHEN transaction_type='refund' THEN ABS(amount) ELSE 0 END), 0) AS total_refunds,
                   COALESCE(SUM(CASE WHEN transaction_type='expense' THEN ABS(amount) ELSE 0 END), 0) AS total_expenses,
                   COALESCE(SUM(CASE WHEN transaction_type='cash_in' THEN amount ELSE 0 END), 0) AS total_cash_in,
                   COALESCE(SUM(CASE WHEN transaction_type='cash_out' THEN ABS(amount) ELSE 0 END), 0) AS total_cash_out
                 FROM shift_transactions WHERE shift_id=$1""",
            shift["id"],
        )
        s = dict(summary)
        expected = (
            shift["opening_cash"]
            + s["total_sales_cash"]
            - s["total_refunds"]
            - s["total_expenses"]
            + s["total_cash_in"]
            - s["total_cash_out"]
        )
        s["expected_cash"] = float(expected)
        recent = await conn.fetch(
            """SELECT transaction_type AS type, amount, payment_method,
                      reference_id, created_at
                 FROM shift_transactions WHERE shift_id=$1
                 ORDER BY created_at DESC LIMIT 20""",
            shift["id"],
        )
    result = dict(shift)
    result["summary"] = s
    result["recent_transactions"] = [dict(r) for r in recent]
    return result


@router.post("/shifts/{shift_id}/close")
async def close_shift(
    shift_id: UUID,
    body: ShiftClose,
    user: UserContext = Depends(require_role("owner", "manager", "cashier")),
):
    uid = _uid(user)
    async with get_transaction() as conn:
        shift = await conn.fetchrow(
            "SELECT * FROM shifts WHERE id=$1 AND user_id=$2 AND status='open'",
            shift_id, uid,
        )
        if not shift:
            raise HTTPException(404, "Open shift not found")
        summary = await conn.fetchrow(
            """SELECT
                   COALESCE(SUM(CASE WHEN transaction_type='sale' AND payment_method='cash' THEN amount ELSE 0 END), 0) AS cash_sales,
                   COALESCE(SUM(CASE WHEN transaction_type='refund' THEN ABS(amount) ELSE 0 END), 0) AS refunds,
                   COALESCE(SUM(CASE WHEN transaction_type='expense' THEN ABS(amount) ELSE 0 END), 0) AS expenses,
                   COALESCE(SUM(CASE WHEN transaction_type='cash_in' THEN amount ELSE 0 END), 0) AS cash_in,
                   COALESCE(SUM(CASE WHEN transaction_type='cash_out' THEN ABS(amount) ELSE 0 END), 0) AS cash_out
                 FROM shift_transactions WHERE shift_id=$1""",
            shift_id,
        )
        expected = (
            shift["opening_cash"]
            + summary["cash_sales"]
            - summary["refunds"]
            - summary["expenses"]
            + summary["cash_in"]
            - summary["cash_out"]
        )
        diff = body.closing_cash - float(expected)
        row = await conn.fetchrow(
            """UPDATE shifts SET
                   status='closed', closed_at=NOW(), closing_cash=$1,
                   expected_cash=$2, cash_difference=$3, notes=$4, closed_by=$5,
                   updated_at=NOW()
               WHERE id=$6 RETURNING *""",
            body.closing_cash, float(expected), diff, body.notes, uid, shift_id,
        )
    return dict(row)


@router.get("/shifts")
async def list_shifts(
    status: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    clauses = ["s.restaurant_id = $1"]
    params: list = [rid]
    if status:
        params.append(status)
        clauses.append(f"s.status = ${len(params)}")
    if user_id:
        params.append(user_id)
        clauses.append(f"s.user_id = ${len(params)}")
    if start_date:
        params.append(start_date)
        clauses.append(f"s.opened_at::date >= ${len(params)}")
    if end_date:
        params.append(end_date)
        clauses.append(f"s.opened_at::date <= ${len(params)}")
    params.extend([limit, offset])
    sql = f"""
        SELECT s.*, cd.name AS drawer_name
          FROM shifts s
          LEFT JOIN cash_drawers cd ON cd.id = s.drawer_id
         WHERE {' AND '.join(clauses)}
         ORDER BY s.opened_at DESC
         LIMIT ${len(params)-1} OFFSET ${len(params)}
    """
    async with get_connection() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# 9. STOCK TRANSFERS
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/transfers")
async def list_transfers(
    status: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    bid = user.branch_id
    clauses = ["st.restaurant_id = $1"]
    params: list = [rid]
    if status:
        params.append(status)
        clauses.append(f"st.status = ${len(params)}")
    if direction and bid:
        if direction == "outgoing":
            params.append(bid)
            clauses.append(f"st.from_branch_id = ${len(params)}::uuid")
        elif direction == "incoming":
            params.append(bid)
            clauses.append(f"st.to_branch_id = ${len(params)}::uuid")
    params.extend([limit, offset])
    sql = f"""
        SELECT st.id, st.transfer_number, st.status,
               fb.name AS from_branch, tb.name AS to_branch,
               st.requested_by, st.shipped_at, st.received_at, st.created_at,
               (SELECT COUNT(*) FROM stock_transfer_items sti WHERE sti.transfer_id = st.id) AS items_count
          FROM stock_transfers st
          LEFT JOIN sub_branches fb ON fb.id = st.from_branch_id
          LEFT JOIN sub_branches tb ON tb.id = st.to_branch_id
         WHERE {' AND '.join(clauses)}
         ORDER BY st.created_at DESC
         LIMIT ${len(params)-1} OFFSET ${len(params)}
    """
    async with get_connection() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


@router.post("/transfers", status_code=201)
async def create_transfer(
    body: TransferCreate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    uid = _uid(user)
    if body.from_branch_id == body.to_branch_id:
        raise HTTPException(400, "Cannot transfer to the same branch")
    async with get_transaction() as conn:
        transfer = await conn.fetchrow(
            """INSERT INTO stock_transfers
                   (restaurant_id, from_branch_id, to_branch_id, requested_by, notes)
               VALUES ($1, $2::uuid, $3::uuid, $4, $5)
               RETURNING *""",
            rid, body.from_branch_id, body.to_branch_id, uid, body.notes,
        )
        for item in body.items:
            await conn.execute(
                """INSERT INTO stock_transfer_items
                       (transfer_id, ingredient_id, quantity_sent, unit, notes)
                   VALUES ($1, $2, $3, $4, $5)""",
                transfer["id"], item.ingredient_id, item.quantity_sent,
                item.unit, item.notes,
            )
    return dict(transfer)


@router.patch("/transfers/{transfer_id}/approve")
async def approve_transfer(
    transfer_id: UUID,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    uid = _uid(user)
    rid = _rid(user)
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """UPDATE stock_transfers SET status='approved', approved_by=$1, updated_at=NOW()
               WHERE id=$2 AND restaurant_id=$3 AND status='draft' RETURNING *""",
            uid, transfer_id, rid,
        )
    if not row:
        raise HTTPException(404, "Transfer not found or not in draft status")
    return dict(row)


@router.patch("/transfers/{transfer_id}/ship")
async def ship_transfer(
    transfer_id: UUID,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    uid = _uid(user)
    rid = _rid(user)
    async with get_transaction() as conn:
        transfer = await conn.fetchrow(
            "SELECT * FROM stock_transfers WHERE id=$1 AND restaurant_id=$2 AND status='approved'",
            transfer_id, rid,
        )
        if not transfer:
            raise HTTPException(404, "Transfer not found or not approved")
        await conn.execute(
            "UPDATE stock_transfers SET status='in_transit', shipped_at=NOW(), updated_at=NOW() WHERE id=$1",
            transfer_id,
        )
        items = await conn.fetch("SELECT * FROM stock_transfer_items WHERE transfer_id=$1", transfer_id)
        for item in items:
            await conn.execute(
                """INSERT INTO inventory_ledger
                       (restaurant_id, branch_id, ingredient_id, transaction_type,
                        quantity_in, quantity_out, reference_type, reference_id, created_by)
                   VALUES ($1, $2, $3, 'transfer_out', 0, $4, 'transfer', $5, $6)""",
                rid, transfer["from_branch_id"], item["ingredient_id"],
                item["quantity_sent"], transfer["transfer_number"], uid,
            )
    return {"status": "in_transit", "transfer_id": str(transfer_id)}


@router.patch("/transfers/{transfer_id}/receive")
async def receive_transfer(
    transfer_id: UUID,
    body: TransferReceive,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    uid = _uid(user)
    rid = _rid(user)
    async with get_transaction() as conn:
        transfer = await conn.fetchrow(
            "SELECT * FROM stock_transfers WHERE id=$1 AND restaurant_id=$2 AND status='in_transit'",
            transfer_id, rid,
        )
        if not transfer:
            raise HTTPException(404, "Transfer not found or not in transit")
        for item in body.items:
            await conn.execute(
                """UPDATE stock_transfer_items SET quantity_received=$1, notes=COALESCE($2, notes)
                   WHERE transfer_id=$3 AND ingredient_id=$4""",
                item.quantity_received, item.notes, transfer_id, item.ingredient_id,
            )
            await conn.execute(
                """INSERT INTO inventory_ledger
                       (restaurant_id, branch_id, ingredient_id, transaction_type,
                        quantity_in, quantity_out, reference_type, reference_id, created_by)
                   VALUES ($1, $2, $3, 'transfer_in', $4, 0, 'transfer', $5, $6)""",
                rid, transfer["to_branch_id"], item.ingredient_id,
                item.quantity_received, transfer["transfer_number"], uid,
            )
        await conn.execute(
            """UPDATE stock_transfers SET status='received', received_by=$1,
                      received_at=NOW(), updated_at=NOW() WHERE id=$2""",
            uid, transfer_id,
        )
    return {"status": "received", "transfer_id": str(transfer_id)}


# ═══════════════════════════════════════════════════════════════════════════
# 10. TAX RATES (GST)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/tax-rates")
async def list_tax_rates(
    is_active: bool = Query(True),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    async with get_connection() as conn:
        rows = await conn.fetch(
            """SELECT id, name, hsn_code, rate_percentage,
                      cgst_percentage, sgst_percentage, igst_percentage,
                      is_inclusive, applicable_on, is_exempt, is_composition, is_active
                 FROM tax_rates
                WHERE restaurant_id=$1 AND is_active=$2
                ORDER BY rate_percentage""",
            rid, is_active,
        )
    return [dict(r) for r in rows]


@router.post("/tax-rates", status_code=201)
async def create_tax_rate(
    body: TaxRateCreate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """INSERT INTO tax_rates
                   (restaurant_id, name, hsn_code, rate_percentage,
                    cgst_percentage, sgst_percentage, igst_percentage,
                    is_inclusive, applicable_on)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *""",
            rid, body.name, body.hsn_code, body.rate_percentage,
            body.cgst_percentage, body.sgst_percentage, body.igst_percentage,
            body.is_inclusive, body.applicable_on,
        )
    return dict(row)


@router.post("/tax-rates/assign", status_code=201)
async def assign_tax(
    body: TaxAssign,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """INSERT INTO item_tax_mapping (item_id, tax_rate_id)
               VALUES ($1, $2::uuid)
               ON CONFLICT (item_id, tax_rate_id) DO NOTHING
               RETURNING *""",
            body.item_id, body.tax_rate_id,
        )
    return dict(row) if row else {"status": "already_assigned"}


@router.delete("/tax-rates/assign/{item_id}/{tax_rate_id}")
async def unassign_tax(
    item_id: int,
    tax_rate_id: UUID,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM item_tax_mapping WHERE item_id=$1 AND tax_rate_id=$2",
            item_id, tax_rate_id,
        )
    return {"status": "removed"}


# ═══════════════════════════════════════════════════════════════════════════
# 11. GST REPORTS
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/gst-reports")
async def list_gst_reports(
    report_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    clauses = ["restaurant_id = $1"]
    params: list = [rid]
    if report_type:
        params.append(report_type)
        clauses.append(f"report_type = ${len(params)}")
    if status:
        params.append(status)
        clauses.append(f"status = ${len(params)}")
    sql = f"""
        SELECT * FROM gst_reports
         WHERE {' AND '.join(clauses)}
         ORDER BY period_start DESC
    """
    async with get_connection() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


@router.post("/gst-reports/generate", status_code=201)
async def generate_gst_report(
    body: GSTReportGenerate,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    async with get_transaction() as conn:
        existing = await conn.fetchrow(
            """SELECT id FROM gst_reports
               WHERE restaurant_id=$1 AND report_type=$2
                 AND period_start=$3 AND period_end=$4""",
            rid, body.report_type, body.period_start, body.period_end,
        )
        if existing:
            await conn.execute(
                "DELETE FROM gst_reports WHERE id=$1", existing["id"],
            )
        # Aggregate from invoices / order_tax_details
        agg = await conn.fetchrow(
            """SELECT
                   COUNT(DISTINCT CASE WHEN i.invoice_type='B2B' THEN i.id END) AS b2b_count,
                   COUNT(DISTINCT CASE WHEN i.invoice_type!='B2B' THEN i.id END) AS b2c_count,
                   COALESCE(SUM(i.total_amount), 0) AS total_sales,
                   COALESCE(SUM(i.taxable_amount), 0) AS total_taxable,
                   COALESCE(SUM(i.cgst_amount), 0) AS cgst_total,
                   COALESCE(SUM(i.sgst_amount), 0) AS sgst_total,
                   COALESCE(SUM(i.igst_amount), 0) AS igst_total
                 FROM invoices i
                WHERE i.restaurant_id = $1
                  AND i.created_at::date >= $2 AND i.created_at::date <= $3
                  AND i.is_cancelled = false""",
            rid, body.period_start, body.period_end,
        )
        total_tax = float(agg["cgst_total"] + agg["sgst_total"] + agg["igst_total"])
        row = await conn.fetchrow(
            """INSERT INTO gst_reports
                   (restaurant_id, report_type, period_start, period_end,
                    total_sales, total_taxable, cgst_total, sgst_total, igst_total,
                    total_tax, b2b_count, b2c_count, status, generated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'generated',NOW())
               RETURNING *""",
            rid, body.report_type, body.period_start, body.period_end,
            agg["total_sales"], agg["total_taxable"],
            agg["cgst_total"], agg["sgst_total"], agg["igst_total"],
            total_tax, agg["b2b_count"], agg["b2c_count"],
        )
    return dict(row)


@router.patch("/gst-reports/{report_id}/filed")
async def mark_filed(
    report_id: UUID,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """UPDATE gst_reports SET status='filed', filed_at=NOW(), updated_at=NOW()
               WHERE id=$1 AND restaurant_id=$2 RETURNING *""",
            report_id, rid,
        )
    if not row:
        raise HTTPException(404, "Report not found")
    return dict(row)


@router.get("/gst-reports/tax-liability")
async def tax_liability(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    if not start_date:
        start_date = date.today().replace(day=1)
    if not end_date:
        end_date = date.today()
    async with get_connection() as conn:
        rows = await conn.fetch(
            """SELECT
                   DATE_TRUNC('month', i.created_at)::date AS month,
                   COALESCE(SUM(i.taxable_amount), 0) AS taxable,
                   COALESCE(SUM(i.cgst_amount), 0) AS cgst,
                   COALESCE(SUM(i.sgst_amount), 0) AS sgst,
                   COALESCE(SUM(i.igst_amount), 0) AS igst,
                   COALESCE(SUM(i.cgst_amount + i.sgst_amount + i.igst_amount), 0) AS total_tax
                 FROM invoices i
                WHERE i.restaurant_id = $1
                  AND i.created_at::date >= $2 AND i.created_at::date <= $3
                  AND i.is_cancelled = false
                GROUP BY DATE_TRUNC('month', i.created_at)
                ORDER BY month DESC""",
            rid, start_date, end_date,
        )
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# 12. ITEM PROFITABILITY
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/profitability")
async def item_profitability(
    branch_id: Optional[str] = Query(None),
    period_start: Optional[date] = Query(None),
    period_end: Optional[date] = Query(None),
    sort_by: str = Query("gross_profit"),
    limit: int = Query(50, le=200),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    bid = _bid(user, branch_id)
    if not period_start:
        period_start = date.today().replace(day=1)
    if not period_end:
        period_end = date.today()
    allowed_sort = {"margin_percent", "gross_profit", "quantity_sold", "total_revenue"}
    if sort_by not in allowed_sort:
        sort_by = "gross_profit"

    clauses = ["ip.restaurant_id = $1", "ip.period_start >= $2", "ip.period_end <= $3"]
    params: list = [rid, period_start, period_end]
    if bid:
        params.append(bid)
        clauses.append(f"ip.branch_id = ${len(params)}::uuid")
    params.append(limit)
    sql = f"""
        SELECT ip.item_id, i."Item_Name" AS item_name,
               SUM(ip.quantity_sold) AS quantity_sold,
               SUM(ip.total_revenue) AS total_revenue,
               SUM(ip.total_cogs) AS total_cogs,
               SUM(ip.gross_profit) AS gross_profit,
               CASE WHEN SUM(ip.total_revenue) > 0
                    THEN ROUND(SUM(ip.gross_profit) * 100.0 / SUM(ip.total_revenue), 2)
                    ELSE 0 END AS margin_percent
          FROM item_profitability ip
          LEFT JOIN items i ON i."Item_ID" = ip.item_id
         WHERE {' AND '.join(clauses)}
         GROUP BY ip.item_id, i."Item_Name"
         ORDER BY {sort_by} DESC
         LIMIT ${len(params)}
    """
    async with get_connection() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# 13. DAILY P&L
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/pnl/daily")
async def daily_pnl(
    branch_id: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    rid = _rid(user)
    bid = _bid(user, branch_id)
    if not start_date:
        start_date = date.today() - timedelta(days=30)
    if not end_date:
        end_date = date.today()
    clauses = ["restaurant_id = $1", "pnl_date >= $2", "pnl_date <= $3"]
    params: list = [rid, start_date, end_date]
    if bid:
        params.append(bid)
        clauses.append(f"branch_id = ${len(params)}::uuid")
    sql = f"""
        SELECT pnl_date, total_revenue, total_cogs, gross_profit,
               operating_expenses, net_profit, tax_collected,
               total_orders, avg_order_value
          FROM daily_pnl
         WHERE {' AND '.join(clauses)}
         ORDER BY pnl_date DESC
    """
    async with get_connection() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]
