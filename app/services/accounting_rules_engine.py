"""
Accounting Rules Engine — Configurable event-to-journal mappings.

Architecture
───────────────────────────────────────────────────────────────────────────────
Instead of hardcoded DR/CR patterns, restaurants can define custom rules
that map business events → journal entries.

Rule evaluation:
  1. Fetch active rules for (restaurant_id, event_type), ordered by priority DESC
  2. For each rule, check if ALL conditions match the event payload
  3. First matching rule wins — creates journal lines from debit/credit accounts
  4. If NO custom rules match → fall back to engine defaults (hardcoded)

Conditions matching:
  - {"method": "cash"} → payload.method == "cash"
  - {"method": ["upi", "card"]} → payload.method IN ["upi", "card"]
  - {"platform": "zomato"} → payload.platform == "zomato"
  - {} → always matches (catch-all)

Usage:
  from app.services.accounting_rules_engine import rules_engine
  lines = await rules_engine.evaluate(restaurant_id, "PAYMENT_COMPLETED", payload)
  if lines:
      await accounting_engine.create_journal_entry(lines=lines, ...)
───────────────────────────────────────────────────────────────────────────────
"""
import json as _json
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from uuid import UUID

from app.core.database import get_connection, get_serializable_transaction
from app.core.exceptions import ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)


def _quantize(val) -> Decimal:
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class AccountingRulesEngine:
    """Evaluate and manage configurable accounting rules."""

    # ── Rule Evaluation ──────────────────────────────────────────────────────

    async def evaluate(
        self,
        restaurant_id: str,
        event_type: str,
        payload: dict,
    ) -> Optional[list[dict]]:
        """
        Evaluate rules for an event. Returns journal lines if a custom rule
        matches, or None if no custom rules apply (caller should use defaults).

        Returns list of {"account": str, "debit": float, "credit": float, "description": str}
        """
        restaurant_uuid = UUID(restaurant_id)

        async with get_connection() as conn:
            rules = await conn.fetch(
                """
                SELECT id, rule_name, debit_account_code, credit_account_code,
                       amount_field, amount_multiplier, conditions,
                       reference_type_override, description_template
                FROM accounting_rules
                WHERE restaurant_id = $1
                  AND event_type = $2
                  AND is_active = true
                ORDER BY priority DESC, created_at ASC
                """,
                restaurant_uuid, event_type,
            )

        if not rules:
            return None  # no custom rules → use engine defaults

        # Find first matching rule
        for rule in rules:
            conditions = rule["conditions"] or {}
            if self._conditions_match(conditions, payload):
                return self._build_lines(rule, payload)

        return None  # no conditions matched → use defaults

    def _conditions_match(self, conditions: dict, payload: dict) -> bool:
        """Check if ALL conditions match the event payload."""
        if not conditions:
            return True  # empty conditions = always match

        for key, expected in conditions.items():
            actual = payload.get(key)
            if actual is None:
                return False

            if isinstance(expected, list):
                # Any-of match
                if str(actual).lower() not in [str(e).lower() for e in expected]:
                    return False
            else:
                # Exact match (case-insensitive for strings)
                if isinstance(expected, str) and isinstance(actual, str):
                    if actual.lower() != expected.lower():
                        return False
                elif actual != expected:
                    return False

        return True

    def _build_lines(self, rule: dict, payload: dict) -> list[dict]:
        """Build journal lines from a matched rule and event payload."""
        amount_field = rule["amount_field"]
        raw_amount = payload.get(amount_field, 0)
        multiplier = Decimal(str(rule["amount_multiplier"]))
        amount = float(_quantize(Decimal(str(raw_amount)) * multiplier))

        if amount <= 0:
            return []

        desc_template = rule["description_template"] or rule["rule_name"]
        try:
            description = desc_template.format(**payload)
        except (KeyError, IndexError):
            description = rule["rule_name"]

        return [
            {
                "account_code": rule["debit_account_code"],
                "debit": amount,
                "credit": 0,
                "description": f"DR: {description}",
            },
            {
                "account_code": rule["credit_account_code"],
                "debit": 0,
                "credit": amount,
                "description": f"CR: {description}",
            },
        ]

    # ── Rule CRUD ────────────────────────────────────────────────────────────

    async def list_rules(
        self,
        restaurant_id: str,
        event_type: Optional[str] = None,
    ) -> list[dict]:
        """List all active rules for a restaurant, optionally filtered by event_type."""
        restaurant_uuid = UUID(restaurant_id)

        async with get_connection() as conn:
            if event_type:
                rows = await conn.fetch(
                    """
                    SELECT id, event_type, rule_name, description,
                           debit_account_code, credit_account_code,
                           amount_field, amount_multiplier, conditions,
                           priority, is_active, reference_type_override,
                           description_template, created_at, updated_at
                    FROM accounting_rules
                    WHERE restaurant_id = $1 AND event_type = $2
                    ORDER BY priority DESC, created_at ASC
                    """,
                    restaurant_uuid, event_type,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, event_type, rule_name, description,
                           debit_account_code, credit_account_code,
                           amount_field, amount_multiplier, conditions,
                           priority, is_active, reference_type_override,
                           description_template, created_at, updated_at
                    FROM accounting_rules
                    WHERE restaurant_id = $1
                    ORDER BY event_type, priority DESC, created_at ASC
                    """,
                    restaurant_uuid,
                )

        return [
            {
                "id": str(r["id"]),
                "event_type": r["event_type"],
                "rule_name": r["rule_name"],
                "description": r["description"],
                "debit_account_code": r["debit_account_code"],
                "credit_account_code": r["credit_account_code"],
                "amount_field": r["amount_field"],
                "amount_multiplier": float(r["amount_multiplier"]),
                "conditions": r["conditions"],
                "priority": r["priority"],
                "is_active": r["is_active"],
                "reference_type_override": r["reference_type_override"],
                "description_template": r["description_template"],
                "created_at": r["created_at"].isoformat(),
                "updated_at": r["updated_at"].isoformat(),
            }
            for r in rows
        ]

    async def create_rule(
        self,
        *,
        restaurant_id: str,
        event_type: str,
        rule_name: str,
        debit_account_code: str,
        credit_account_code: str,
        amount_field: str = "amount",
        amount_multiplier: float = 1.0,
        conditions: Optional[dict] = None,
        priority: int = 100,
        description: str = "",
        reference_type_override: Optional[str] = None,
        description_template: Optional[str] = None,
    ) -> dict:
        """Create a new accounting rule with safety validations."""
        restaurant_uuid = UUID(restaurant_id)

        # SAFETY: debit and credit accounts must differ
        if debit_account_code == credit_account_code:
            raise ValidationError(
                f"Debit and credit accounts cannot be the same ({debit_account_code}). "
                "This would create a zero-effect journal entry."
            )

        # SAFETY: multiplier must be positive
        if amount_multiplier <= 0:
            raise ValidationError("amount_multiplier must be > 0")

        # Validate accounts exist
        async with get_connection() as conn:
            for code in [debit_account_code, credit_account_code]:
                exists = await conn.fetchval(
                    "SELECT 1 FROM chart_of_accounts "
                    "WHERE restaurant_id = $1 AND account_code = $2 AND is_active = true",
                    restaurant_uuid, code,
                )
                if not exists:
                    raise ValidationError(
                        f"Account code '{code}' not found in chart of accounts"
                    )

            # SAFETY: detect duplicate priority for same event_type + conditions
            existing_same_priority = await conn.fetchrow(
                """SELECT id, rule_name FROM accounting_rules
                   WHERE restaurant_id = $1 AND event_type = $2
                     AND priority = $3 AND is_active = true""",
                restaurant_uuid, event_type, priority,
            )
            if existing_same_priority:
                logger.warning(
                    "rule_priority_conflict",
                    new_rule=rule_name,
                    existing_rule=existing_same_priority["rule_name"],
                    priority=priority,
                    event_type=event_type,
                )
                # Warn but allow — first-inserted wins at same priority

        async with get_serializable_transaction() as conn:
            rule_id = await conn.fetchval(
                """
                INSERT INTO accounting_rules
                    (restaurant_id, event_type, rule_name, description,
                     debit_account_code, credit_account_code,
                     amount_field, amount_multiplier, conditions,
                     priority, reference_type_override, description_template)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12)
                RETURNING id
                """,
                restaurant_uuid, event_type, rule_name, description,
                debit_account_code, credit_account_code,
                amount_field, amount_multiplier,
                _json.dumps(conditions or {}),
                priority, reference_type_override, description_template,
            )

        logger.info(
            "accounting_rule_created",
            rule_id=str(rule_id),
            restaurant_id=restaurant_id,
            event_type=event_type,
            rule_name=rule_name,
        )

        return {
            "id": str(rule_id),
            "event_type": event_type,
            "rule_name": rule_name,
        }

    async def update_rule(
        self,
        *,
        rule_id: str,
        restaurant_id: str,
        **fields,
    ) -> dict:
        """Update specific fields on a rule."""
        rule_uuid = UUID(rule_id)
        restaurant_uuid = UUID(restaurant_id)

        allowed_fields = {
            "rule_name", "description", "debit_account_code", "credit_account_code",
            "amount_field", "amount_multiplier", "conditions", "priority",
            "is_active", "reference_type_override", "description_template",
        }

        updates = {k: v for k, v in fields.items() if k in allowed_fields and v is not None}
        if not updates:
            raise ValidationError("No valid fields to update")

        # Validate account codes if being changed
        if "debit_account_code" in updates or "credit_account_code" in updates:
            async with get_connection() as conn:
                for key in ["debit_account_code", "credit_account_code"]:
                    if key in updates:
                        exists = await conn.fetchval(
                            "SELECT 1 FROM chart_of_accounts "
                            "WHERE restaurant_id = $1 AND account_code = $2 AND is_active = true",
                            restaurant_uuid, updates[key],
                        )
                        if not exists:
                            raise ValidationError(
                                f"Account code '{updates[key]}' not found"
                            )

        set_clauses = []
        params = [rule_uuid, restaurant_uuid]
        idx = 3
        for key, val in updates.items():
            if key == "conditions":
                set_clauses.append(f"{key} = ${idx}::jsonb")
                params.append(_json.dumps(val))
            else:
                set_clauses.append(f"{key} = ${idx}")
                params.append(val)
            idx += 1

        set_clauses.append("updated_at = NOW()")

        async with get_serializable_transaction() as conn:
            result = await conn.fetchrow(
                f"""
                UPDATE accounting_rules
                SET {', '.join(set_clauses)}
                WHERE id = $1 AND restaurant_id = $2
                RETURNING id, rule_name, event_type
                """,
                *params,
            )

        if not result:
            raise ValidationError("Rule not found")

        return {
            "id": str(result["id"]),
            "rule_name": result["rule_name"],
            "event_type": result["event_type"],
            "updated": True,
        }

    async def delete_rule(self, *, rule_id: str, restaurant_id: str) -> dict:
        """Soft-delete a rule (set is_active=false)."""
        rule_uuid = UUID(rule_id)
        restaurant_uuid = UUID(restaurant_id)

        async with get_connection() as conn:
            result = await conn.execute(
                "UPDATE accounting_rules SET is_active = false, updated_at = NOW() "
                "WHERE id = $1 AND restaurant_id = $2",
                rule_uuid, restaurant_uuid,
            )

        return {"id": rule_id, "deleted": True}


# Singleton
rules_engine = AccountingRulesEngine()
