"""
Onboarding plan selection + Razorpay SaaS-subscription orchestration.

Responsibilities
----------------
* Expose the server-authoritative plan catalog (pricing is GST-EXCLUSIVE).
* Persist the merchant's selected plan on ``restaurants.plan``.
* Create / reuse a per-merchant Razorpay subscription for the recurring
  "Software" plans (starter, business) and verify the Checkout callback.
* Keep ``merchant_subscriptions`` in sync from ``subscription.*`` webhooks.
* Compose the onboarding-state object the FE uses to gate navigation:
  the merchant may only proceed to restaurant-settings + KYC once the
  subscription gate is satisfied (paid for software plans; auto-satisfied
  for the ₹0 integrated-payments plans).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.core.config import get_settings
from app.core.database import get_connection, get_service_connection
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.services.razorpay import subscriptions as rzp_subscriptions
from app.services.razorpay.payments import verify_subscription_payment_signature

logger = get_logger(__name__)


# ── Plan catalog (server-authoritative; prices EXCLUDE GST) ─────────────────

PLAN_CATALOG: dict[str, dict[str, Any]] = {
    "starter": {
        "id": "starter",
        "display_name": "Software",
        "subscription_amount_year": 5000,
        "device_one_time": 0,
        "txn_fee_percent": 0.0,
        "integrated_payments": False,
        "requires_subscription": True,
        "requires_device": False,
        "description": "₹5,000 / year. Payments via merchant's own UPI.",
    },
    "business": {
        "id": "business",
        "display_name": "Software + Device",
        "subscription_amount_year": 5000,
        "device_one_time": 30000,
        "txn_fee_percent": 0.0,
        "integrated_payments": False,
        "requires_subscription": True,
        "requires_device": True,
        "description": "₹30,000 one-time device + ₹5,000 / year. Own UPI.",
    },
    "growth": {
        "id": "growth",
        "display_name": "Integrated Payments",
        "subscription_amount_year": 0,
        "device_one_time": 0,
        "txn_fee_percent": 1.75,
        "integrated_payments": True,
        "requires_subscription": False,
        "requires_device": False,
        "description": "₹0 subscription, 1.75% per transaction. Auto reconciliation.",
    },
    "enterprise": {
        "id": "enterprise",
        "display_name": "Complete Suite",
        "subscription_amount_year": 0,
        "device_one_time": 30000,
        "txn_fee_percent": 1.75,
        "integrated_payments": True,
        "requires_subscription": False,
        "requires_device": True,
        "description": "₹30,000 one-time device + 1.75% per transaction.",
    },
}

DEFAULT_PLAN = "growth"
PRICES_EXCLUDE_GST = True

# Statuses we treat as "the SaaS subscription has been paid / gate unlocked".
PAID_STATUSES = frozenset({"authenticated", "active"})
# Statuses that mean we can reuse an existing subscription row instead of
# creating a brand-new Razorpay subscription.
REUSABLE_STATUSES = frozenset({"created", "authenticated", "active", "pending"})


def get_plan_catalog() -> dict[str, Any]:
    """Return the full plan catalog plus billing metadata for the FE."""
    return {
        "default_plan": DEFAULT_PLAN,
        "prices_exclude_gst": PRICES_EXCLUDE_GST,
        "currency": "INR",
        "plans": list(PLAN_CATALOG.values()),
    }


def _plan_meta(plan: Optional[str]) -> Optional[dict[str, Any]]:
    return PLAN_CATALOG.get(plan) if plan else None


def _rzp_plan_id_for(plan: str) -> Optional[str]:
    settings = get_settings()
    return {
        "starter": settings.RZP_PLAN_ID_STARTER,
        "business": settings.RZP_PLAN_ID_BUSINESS,
    }.get(plan)


class SubscriptionService:
    # ── merchant / restaurant resolution ───────────────────────────────────

    async def _resolve_restaurant(self, user) -> dict:
        """Return {restaurant_id, owner_id, plan} for the caller's restaurant."""
        owner_id = user.owner_id if getattr(user, "is_branch_user", False) else user.user_id
        async with get_connection() as conn:
            row = None
            if getattr(user, "restaurant_id", None):
                row = await conn.fetchrow(
                    "SELECT id::text AS restaurant_id, owner_id, plan FROM restaurants WHERE id = $1::uuid",
                    user.restaurant_id,
                )
            if row is None:
                row = await conn.fetchrow(
                    """
                    SELECT id::text AS restaurant_id, owner_id, plan
                    FROM restaurants
                    WHERE owner_id = $1
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    owner_id,
                )
        if row is None:
            raise NotFoundError("Restaurant")
        return dict(row)

    # ── plan persistence ────────────────────────────────────────────────────

    async def set_plan(self, user, plan: str) -> dict:
        plan = (plan or "").strip().lower()
        if plan not in PLAN_CATALOG:
            raise ValidationError(
                f"Unknown plan '{plan}'. Supported: {', '.join(PLAN_CATALOG)}."
            )
        rest = await self._resolve_restaurant(user)
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE restaurants SET plan = $1, updated_at = now() WHERE id = $2::uuid",
                plan,
                rest["restaurant_id"],
            )
        logger.info(
            "onboarding_plan_set",
            restaurant_id=rest["restaurant_id"],
            user_id=str(user.user_id),
            plan=plan,
        )
        return await self.get_onboarding_state(user)

    # ── subscription create / reuse ──────────────────────────────────────────

    async def _latest_subscription_row(self, restaurant_id: str) -> Optional[dict]:
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM merchant_subscriptions
                WHERE restaurant_id = $1::uuid
                ORDER BY created_at DESC
                LIMIT 1
                """,
                restaurant_id,
            )
        return dict(row) if row else None

    async def create_or_get_subscription(self, user) -> dict:
        rest = await self._resolve_restaurant(user)
        plan = rest.get("plan")
        if not plan:
            raise ValidationError("Select a plan before starting subscription payment.")
        meta = PLAN_CATALOG[plan]

        if not meta["requires_subscription"]:
            # Integrated-payments plans: nothing to pay upfront.
            return {
                "required": False,
                "plan": plan,
                "reason": "This plan has no upfront subscription (₹0). Proceed to settings.",
            }

        rzp_plan_id = _rzp_plan_id_for(plan)
        if not rzp_plan_id:
            raise ValidationError(
                f"No Razorpay plan id configured for '{plan}'. "
                "Set RZP_PLAN_ID_STARTER / RZP_PLAN_ID_BUSINESS."
            )

        # Reuse an in-flight / paid subscription for the same plan if present.
        existing = await self._latest_subscription_row(rest["restaurant_id"])
        if (
            existing
            and existing["plan"] == plan
            and existing["status"] in REUSABLE_STATUSES
            and existing["razorpay_plan_id"] == rzp_plan_id
        ):
            return self._subscription_public(existing, required=True)

        settings = get_settings()
        sub = await rzp_subscriptions.create_subscription(
            plan_id=rzp_plan_id,
            total_count=settings.RZP_SUBSCRIPTION_TOTAL_COUNT,
            customer_notify=True,
            notes={
                "restaurant_id": rest["restaurant_id"],
                "user_id": str(user.user_id),
                "plan": plan,
                "source": "onboarding",
            },
            merchant_id=rest["restaurant_id"],
        )

        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO merchant_subscriptions (
                    restaurant_id, user_id, plan,
                    razorpay_plan_id, razorpay_subscription_id, razorpay_customer_id,
                    status, short_url, total_count, remaining_count, notes, raw
                ) VALUES (
                    $1::uuid, $2, $3,
                    $4, $5, $6,
                    $7, $8, $9, $10, $11::jsonb, $12::jsonb
                )
                ON CONFLICT (razorpay_subscription_id) DO UPDATE SET
                    status     = EXCLUDED.status,
                    short_url  = EXCLUDED.short_url,
                    raw        = EXCLUDED.raw,
                    updated_at = now()
                RETURNING *
                """,
                rest["restaurant_id"],
                str(user.user_id),
                plan,
                rzp_plan_id,
                sub.get("id"),
                sub.get("customer_id"),
                sub.get("status") or "created",
                sub.get("short_url"),
                sub.get("total_count"),
                sub.get("remaining_count"),
                json.dumps(sub.get("notes") or {}),
                json.dumps(sub),
            )
        logger.info(
            "onboarding_subscription_created",
            restaurant_id=rest["restaurant_id"],
            plan=plan,
            razorpay_subscription_id=sub.get("id"),
        )
        return self._subscription_public(dict(row), required=True)

    # ── verify the Checkout callback ──────────────────────────────────────────

    async def verify_subscription_payment(
        self,
        user,
        *,
        razorpay_payment_id: str,
        razorpay_subscription_id: str,
        razorpay_signature: str,
    ) -> dict:
        if not verify_subscription_payment_signature(
            razorpay_payment_id=razorpay_payment_id,
            razorpay_subscription_id=razorpay_subscription_id,
            signature=razorpay_signature,
        ):
            raise ValidationError("Invalid subscription payment signature.")

        rest = await self._resolve_restaurant(user)

        # Pull the authoritative current status from Razorpay (best-effort).
        latest_status = "authenticated"
        raw: dict[str, Any] = {}
        try:
            entity = await rzp_subscriptions.fetch_subscription(
                razorpay_subscription_id, merchant_id=rest["restaurant_id"]
            )
            latest_status = entity.get("status") or latest_status
            raw = entity
        except Exception:  # noqa: BLE001
            logger.warning(
                "subscription_fetch_after_verify_failed",
                razorpay_subscription_id=razorpay_subscription_id,
            )

        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                UPDATE merchant_subscriptions
                SET status           = $2,
                    authenticated_at = COALESCE(authenticated_at, now()),
                    activated_at     = CASE WHEN $2 = 'active' THEN COALESCE(activated_at, now())
                                            ELSE activated_at END,
                    last_payment_id  = $3,
                    paid_count       = GREATEST(paid_count, 1),
                    raw              = CASE WHEN $4::jsonb = '{}'::jsonb THEN raw ELSE $4::jsonb END,
                    updated_at       = now()
                WHERE razorpay_subscription_id = $1
                  AND restaurant_id = $5::uuid
                RETURNING *
                """,
                razorpay_subscription_id,
                latest_status,
                razorpay_payment_id,
                json.dumps(raw),
                rest["restaurant_id"],
            )
        if row is None:
            raise NotFoundError("Subscription")

        logger.info(
            "onboarding_subscription_verified",
            restaurant_id=rest["restaurant_id"],
            razorpay_subscription_id=razorpay_subscription_id,
            status=latest_status,
        )
        return await self.get_onboarding_state(user)

    # ── onboarding state composition ──────────────────────────────────────────

    async def get_onboarding_state(self, user) -> dict:
        rest = await self._resolve_restaurant(user)
        plan = rest.get("plan")
        meta = _plan_meta(plan)
        sub_row = await self._latest_subscription_row(rest["restaurant_id"])

        requires_subscription = bool(meta and meta["requires_subscription"])
        sub_status = sub_row["status"] if sub_row else None
        subscription_paid = (not requires_subscription) or (
            sub_status in PAID_STATUSES
        )

        kyc_status = await self._kyc_status(rest["restaurant_id"])
        kyc_submitted = kyc_status not in (None, "NOT_SUBMITTED")

        plan_selected = plan is not None
        can_proceed_to_settings = plan_selected and subscription_paid
        onboarding_complete = can_proceed_to_settings and kyc_submitted

        return {
            "restaurant_id": rest["restaurant_id"],
            "plan": plan,
            "plan_meta": meta,
            "requires_subscription": requires_subscription,
            "subscription": self._subscription_public(sub_row, required=requires_subscription)
            if sub_row
            else {"required": requires_subscription, "status": None},
            "subscription_paid": subscription_paid,
            "kyc_status": kyc_status,
            "steps": {
                "plan_selected": plan_selected,
                "subscription_paid": subscription_paid,
                "kyc_submitted": kyc_submitted,
            },
            "can_proceed_to_settings": can_proceed_to_settings,
            "onboarding_complete": onboarding_complete,
        }

    async def _kyc_status(self, restaurant_id: str) -> Optional[str]:
        try:
            from app.services.razorpay.kyc_batch_service import rzp_kyc_batch_service
            status = await rzp_kyc_batch_service.get_merchant_status(restaurant_id)
            return (status or {}).get("status")
        except Exception:  # noqa: BLE001
            logger.warning("onboarding_kyc_status_failed", restaurant_id=restaurant_id)
            return None

    @staticmethod
    def _subscription_public(row: Optional[dict], *, required: bool) -> dict:
        if not row:
            return {"required": required, "status": None}
        return {
            "required": required,
            "id": str(row["id"]),
            "plan": row["plan"],
            "status": row["status"],
            "razorpay_subscription_id": row["razorpay_subscription_id"],
            "razorpay_plan_id": row["razorpay_plan_id"],
            "short_url": row.get("short_url"),
            "paid": row["status"] in PAID_STATUSES,
            "key_id": get_settings().RAZORPAY_KEY_ID,
        }

    # ── webhook sync (called from the dispatcher; no tenant context) ──────────

    async def handle_subscription_webhook(
        self, *, event: str, envelope: dict
    ) -> dict:
        payload = envelope.get("payload") or {}
        sub_entity = ((payload.get("subscription") or {}).get("entity")) or {}
        pay_entity = ((payload.get("payment") or {}).get("entity")) or {}
        sub_id = sub_entity.get("id")
        if not sub_id:
            return {"status": "skipped", "reason": "no_subscription_id"}

        status = sub_entity.get("status") or _status_from_event(event)
        payment_id = pay_entity.get("id")

        async with get_service_connection() as conn:
            # Append-only audit first.
            await conn.execute(
                """
                INSERT INTO merchant_subscription_events
                    (razorpay_subscription_id, event, razorpay_payment_id, status, raw)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                """,
                sub_id, event, payment_id, status, json.dumps(envelope),
            )

            # State-forward update of an existing mirror row. We never insert
            # from a webhook (restaurant_id is unknown unless we created it),
            # except when the entity carries our onboarding notes.
            updated = await conn.fetchrow(
                """
                UPDATE merchant_subscriptions
                SET status           = $2,
                    paid_count       = GREATEST(paid_count, COALESCE($3, paid_count)),
                    remaining_count  = COALESCE($4, remaining_count),
                    charge_at        = COALESCE(CASE WHEN $5::bigint IS NULL THEN NULL
                                                     ELSE to_timestamp($5::bigint) END, charge_at),
                    current_start    = COALESCE(CASE WHEN $6::bigint IS NULL THEN NULL
                                                     ELSE to_timestamp($6::bigint) END, current_start),
                    current_end      = COALESCE(CASE WHEN $7::bigint IS NULL THEN NULL
                                                     ELSE to_timestamp($7::bigint) END, current_end),
                    authenticated_at = CASE WHEN $2 IN ('authenticated','active')
                                            THEN COALESCE(authenticated_at, now()) ELSE authenticated_at END,
                    activated_at     = CASE WHEN $2 = 'active'
                                            THEN COALESCE(activated_at, now()) ELSE activated_at END,
                    cancelled_at     = CASE WHEN $2 = 'cancelled'
                                            THEN COALESCE(cancelled_at, now()) ELSE cancelled_at END,
                    last_payment_id  = COALESCE($8, last_payment_id),
                    raw              = $9::jsonb,
                    updated_at       = now()
                WHERE razorpay_subscription_id = $1
                RETURNING restaurant_id::text AS restaurant_id
                """,
                sub_id,
                status,
                sub_entity.get("paid_count"),
                sub_entity.get("remaining_count"),
                sub_entity.get("charge_at"),
                sub_entity.get("current_start"),
                sub_entity.get("current_end"),
                payment_id,
                json.dumps(sub_entity),
            )

            if updated is None:
                # Self-heal: if Razorpay notes carry our restaurant_id, create
                # the mirror row so future state is tracked.
                notes = sub_entity.get("notes") or {}
                rid = notes.get("restaurant_id") if isinstance(notes, dict) else None
                plan = notes.get("plan") if isinstance(notes, dict) else None
                if rid and plan:
                    await conn.execute(
                        """
                        INSERT INTO merchant_subscriptions (
                            restaurant_id, user_id, plan, razorpay_plan_id,
                            razorpay_subscription_id, razorpay_customer_id,
                            status, total_count, remaining_count, raw
                        ) VALUES (
                            $1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb
                        )
                        ON CONFLICT (razorpay_subscription_id) DO NOTHING
                        """,
                        rid,
                        notes.get("user_id"),
                        plan,
                        sub_entity.get("plan_id"),
                        sub_id,
                        sub_entity.get("customer_id"),
                        status,
                        sub_entity.get("total_count"),
                        sub_entity.get("remaining_count"),
                        json.dumps(sub_entity),
                    )

        logger.info(
            "subscription_webhook_processed",
            event=event,
            razorpay_subscription_id=sub_id,
            status=status,
            matched=bool(updated),
        )
        return {"status": "processed", "subscription_id": sub_id, "new_status": status}


def _status_from_event(event: str) -> str:
    return {
        "subscription.authenticated": "authenticated",
        "subscription.activated": "active",
        "subscription.charged": "active",
        "subscription.pending": "pending",
        "subscription.halted": "halted",
        "subscription.cancelled": "cancelled",
        "subscription.completed": "completed",
        "subscription.paused": "paused",
        "subscription.resumed": "active",
        "subscription.updated": "active",
    }.get(event, "created")


subscription_service = SubscriptionService()
