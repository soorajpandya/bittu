"""Centralized scope catalog for endpoint-level RBAC.

Scopes are *what an endpoint requires*. Roles are *what a user has*.
The mapping role→scopes lives in app.core.auth.permissions (legacy:
ROLE_PERMISSIONS in app.core.auth).

Adding a new role should not require touching any endpoint — only the
role→scope matrix.

See docs/ARCHITECTURE_V2.md §7 (RBAC Architecture).
"""
from __future__ import annotations

from typing import Final


# ── Platform scopes ───────────────────────────────────────────────────
PLATFORM_MERCHANTS_READ:     Final = "platform:merchants:read"
PLATFORM_MERCHANTS_WRITE:    Final = "platform:merchants:write"
PLATFORM_KYC_REVIEW:         Final = "platform:kyc:review"
PLATFORM_FEE_PLANS_WRITE:    Final = "platform:fee_plans:write"
PLATFORM_PAYOUTS_APPROVE:    Final = "platform:payouts:approve"
PLATFORM_ESCROW_READ:        Final = "platform:escrow:read"
PLATFORM_RECON_OPERATE:      Final = "platform:recon:operate"
PLATFORM_DISPUTES_OPERATE:   Final = "platform:disputes:operate"
PLATFORM_REFUNDS_OPERATE:    Final = "platform:refunds:operate"
PLATFORM_AUDIT_READ:         Final = "platform:audit:read"
PLATFORM_RISK_OPERATE:       Final = "platform:risk:operate"
PLATFORM_FIN_REPORTS_READ:   Final = "platform:fin_reports:read"

# ── Merchant scopes ───────────────────────────────────────────────────
MERCHANT_ORDERS_READ:        Final = "merchant:orders:read"
MERCHANT_ORDERS_WRITE:       Final = "merchant:orders:write"
MERCHANT_MENU_READ:          Final = "merchant:menu:read"
MERCHANT_MENU_WRITE:         Final = "merchant:menu:write"
MERCHANT_INVENTORY_READ:     Final = "merchant:inventory:read"
MERCHANT_INVENTORY_WRITE:    Final = "merchant:inventory:write"
MERCHANT_STAFF_READ:         Final = "merchant:staff:read"
MERCHANT_STAFF_WRITE:        Final = "merchant:staff:write"
MERCHANT_LEDGER_READ:        Final = "merchant:ledger:read"
MERCHANT_PAYOUTS_READ:       Final = "merchant:payouts:read"
MERCHANT_STATEMENTS_READ:    Final = "merchant:statements:read"
MERCHANT_REPORTS_READ:       Final = "merchant:reports:read"
MERCHANT_SETTINGS_WRITE:     Final = "merchant:settings:write"

# ── Branch scopes ─────────────────────────────────────────────────────
BRANCH_POS_OPERATE:          Final = "branch:pos:operate"
BRANCH_KITCHEN_OPERATE:      Final = "branch:kitchen:operate"
BRANCH_TABLES_OPERATE:       Final = "branch:tables:operate"
BRANCH_INVENTORY_USE:        Final = "branch:inventory:use"

# ── Internal / financial scopes ───────────────────────────────────────
INTERNAL_RECON_WRITE:        Final = "internal:recon:write"
INTERNAL_SETTLEMENT_WRITE:   Final = "internal:settlement:write"
INTERNAL_PAYOUT_WRITE:       Final = "internal:payout:write"
INTERNAL_WEBHOOK_PROCESS:    Final = "internal:webhook:process"
INTERNAL_RISK_WRITE:         Final = "internal:risk:write"

FINANCIAL_LEDGER_WRITE:      Final = "financial:ledger:write"
FINANCIAL_LEDGER_READ:       Final = "financial:ledger:read"
FINANCIAL_JOURNAL_POST:      Final = "financial:journal:post"
FINANCIAL_PAYOUT_ORCHESTRATE:Final = "financial:payout:orchestrate"
FINANCIAL_REFUND_ORCHESTRATE:Final = "financial:refund:orchestrate"


__all__ = [n for n in globals() if n.isupper()]
