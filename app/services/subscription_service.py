"""
Subscription & Billing Service.

Handles:
  - Trial management with eligibility checks
  - Razorpay subscription lifecycle
  - Payment webhook processing for recurring billing
  - Grace period and suspension logic

State machine:
  TRIAL → ACTIVE → PAST_DUE → GRACE_PERIOD → SUSPENDED → CANCELLED
  Any paid state → ACTIVE on successful payment

Security:
  - Subscription checks on every API request (middleware)
  - Grace period allows continued access during payment retry
  - No data deletion on cancellation, just access restriction
"""
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.core.auth import UserContext
from app.core.database import get_connection, get_serializable_transaction
from app.core.redis import DistributedLock, LockError, cache_set, cache_get, cache_delete
from app.core.state_machines import SubscriptionStatus
from app.core.events import (
    DomainEvent, emit_and_publish,
    SUBSCRIPTION_ACTIVATED, SUBSCRIPTION_PAYMENT_FAILED, SUBSCRIPTION_CANCELLED,
)
from app.core.exceptions import NotFoundError, PaymentError, ValidationError
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class SubscriptionService:

    async def check_active(self, user_id: str) -> bool:
        """
        Check if a user has an active subscription.
        Cached in Redis for performance (checked on every API request).
        """
        cache_key = f"sub_active:{user_id}"
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached == "true"

        async with get_connection() as conn:
            sub = await conn.fetchrow(
                """
                SELECT id, status, trial_expires_at, current_period_end, grace_period_end
                FROM user_subscriptions
                WHERE user_id = $1
                ORDER BY created_at DESC LIMIT 1
                """,
                user_id,
            )

            if not sub:
                # Check trial eligibility
                trial = await conn.fetchrow(
                    "SELECT trial_expires_at FROM trial_eligibility WHERE user_id = $1",
                    user_id,
                )
                if trial and trial["trial_expires_at"] > datetime.now(timezone.utc):
                    await cache_set(cache_key, "true", ttl=300)
                    return True
                await cache_set(cache_key, "false", ttl=60)
                return False

            now = datetime.now(timezone.utc)
            status = sub["status"]
            is_active = False

            if status == "ACTIVE":
                is_active = True
            elif status in ("TRIAL", "trialing"):
                # Trial must not have expired
                if sub.get("trial_expires_at"):
                    is_active = sub["trial_expires_at"] > now
                else:
                    is_active = True
            elif status == "PAST_DUE" and sub.get("grace_period_end"):
                is_active = sub["grace_period_end"] > now
            elif status == "GRACE_PERIOD" and sub.get("grace_period_end"):
                is_active = sub["grace_period_end"] > now

            await cache_set(cache_key, "true" if is_active else "false", ttl=300)
            return is_active

    async def get_plans(self) -> list[dict]:
        """List all available subscription plans."""
        try:
            async with get_connection() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, name, description, price, currency, interval, features, is_active, sort_order
                    FROM subscription_plans
                    WHERE is_active = true
                    ORDER BY sort_order ASC, price ASC
                    """
                )
                if not rows:
                    return []
                return [dict(r) for r in rows]
        except Exception:
            # Table may not have data yet
            return []

    async def verify_subscription(self, user: UserContext) -> dict:
        """Verify and return the user's current subscription status."""
        try:
            is_active = await self.check_active(user.user_id)
        except Exception as e:
            logger.error(f"Failed to check subscription status for {user.user_id}: {e}")
            is_active = False

        try:
            sub = await self.get_subscription(user)
        except Exception as e:
            logger.error(f"Failed to get subscription for {user.user_id}: {e}")
            sub = None

        return {
            "is_active": is_active,
            "active": is_active,
            "subscription": sub,
            "user_id": user.user_id,
        }

    async def start_free_trial(self, user: UserContext) -> dict:
        """Start a 14-day free trial. One trial per user."""
        async with get_serializable_transaction() as conn:
            existing = await conn.fetchrow(
                "SELECT id, status, trial_used FROM user_subscriptions WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
                user.user_id,
            )
            if existing and existing["trial_used"]:
                raise ValidationError("Free trial already used")
            if existing and existing["status"] in ("ACTIVE", "trialing", "TRIAL"):
                raise ValidationError("You already have an active subscription")

            now = datetime.now(timezone.utc)
            trial_end = now + timedelta(days=14)

            if existing:
                await conn.execute(
                    """
                    UPDATE user_subscriptions
                    SET status = 'trialing', trial_started_at = $1, trial_expires_at = $2,
                        trial_end = $2, trial_used = true, updated_at = $1
                    WHERE id = $3
                    """,
                    now, trial_end, existing["id"],
                )
                sub_id = existing["id"]
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO user_subscriptions
                        (user_id, status, trial_started_at, trial_expires_at, trial_end, trial_used, created_at, updated_at)
                    VALUES ($1, 'trialing', $2, $3, $3, true, $2, $2)
                    RETURNING id
                    """,
                    user.user_id, now, trial_end,
                )
                sub_id = row["id"]

            # Also record in trial_eligibility
            await conn.execute(
                """
                INSERT INTO trial_eligibility (user_id, trial_started_at, trial_expires_at)
                VALUES ($1, $2, $3)
                ON CONFLICT DO NOTHING
                """,
                user.user_id, now, trial_end,
            )

            await cache_delete(f"sub_active:{user.user_id}")

        return {
            "subscription_id": sub_id,
            "status": "trialing",
            "trial_started_at": now.isoformat(),
            "trial_expires_at": trial_end.isoformat(),
        }

    async def get_subscription(self, user: UserContext) -> Optional[dict]:
        """Get user's current subscription details."""
        async with get_connection() as conn:
            sub = await conn.fetchrow(
                """
                SELECT us.*, sp.name as plan_name, sp.price as plan_price,
                       sp.features as plan_features
                FROM user_subscriptions us
                LEFT JOIN subscription_plans sp ON sp.id = us.plan_id
                WHERE us.user_id = $1
                ORDER BY us.created_at DESC LIMIT 1
                """,
                user.user_id,
            )
            return dict(sub) if sub else None

    async def handle_payment_webhook(
        self,
        event_type: str,
        payload: dict,
    ) -> dict:
        """
        Handle Razorpay subscription webhooks.
        Idempotent: same webhook ID processed only once.
        """
        from app.core.redis import check_idempotency, set_idempotency

        event_id = payload.get("event_id", payload.get("id", ""))
        existing = await check_idempotency(f"sub_webhook:{event_id}")
        if existing:
            return {"status": "already_processed"}

        entity = payload.get("payload", {})

        if event_type == "subscription.authenticated":
            await self._handle_authenticated(entity)
        elif event_type == "subscription.activated":
            await self._handle_activated(entity)
        elif event_type == "subscription.charged":
            await self._handle_charged(entity)
        elif event_type == "subscription.payment_failed":
            await self._handle_payment_failed(entity)
        elif event_type == "subscription.cancelled":
            await self._handle_cancelled(entity)

        await set_idempotency(f"sub_webhook:{event_id}", "processed", ttl=86400)
        return {"status": "processed"}

    async def _handle_activated(self, entity: dict):
        sub_entity = entity.get("subscription", {}).get("entity", {})
        rz_sub_id = sub_entity.get("id")
        if not rz_sub_id:
            return

        async with get_serializable_transaction() as conn:
            sub = await conn.fetchrow(
                "SELECT id, user_id FROM user_subscriptions WHERE razorpay_subscription_id = $1 FOR UPDATE",
                rz_sub_id,
            )
            if not sub:
                return

            now = datetime.now(timezone.utc)
            await conn.execute(
                """
                UPDATE user_subscriptions
                SET status = 'ACTIVE', current_period_start = $1,
                    last_payment_at = $1, updated_at = $1
                WHERE id = $2
                """,
                now, sub["id"],
            )

            await cache_delete(f"sub_active:{sub['user_id']}")

        await emit_and_publish(DomainEvent(
            event_type=SUBSCRIPTION_ACTIVATED,
            payload={"subscription_id": str(sub["id"]), "user_id": str(sub["user_id"])},
        ))

    async def _handle_authenticated(self, entity: dict):
        pass  # Log only

    async def _handle_charged(self, entity: dict):
        payment_entity = entity.get("payment", {}).get("entity", {})
        sub_entity = entity.get("subscription", {}).get("entity", {})
        rz_sub_id = sub_entity.get("id") if sub_entity else None
        rz_payment_id = payment_entity.get("id")
        amount = payment_entity.get("amount", 0) / 100  # paise to rupees

        if not rz_sub_id:
            return

        async with get_serializable_transaction() as conn:
            sub = await conn.fetchrow(
                "SELECT id, user_id FROM user_subscriptions WHERE razorpay_subscription_id = $1 FOR UPDATE",
                rz_sub_id,
            )
            if not sub:
                return

            now = datetime.now(timezone.utc)
            await conn.execute(
                """
                UPDATE user_subscriptions
                SET status = 'ACTIVE', last_payment_at = $1, payment_retry_count = 0, updated_at = $1
                WHERE id = $2
                """,
                now, sub["id"],
            )

            # Record billing history
            await conn.execute(
                """
                INSERT INTO billing_history (user_id, subscription_id, razorpay_payment_id, amount, status, paid_at)
                VALUES ($1, $2, $3, $4, 'paid', $5)
                """,
                str(sub["user_id"]), sub["id"], rz_payment_id, amount, now,
            )

            await cache_delete(f"sub_active:{sub['user_id']}")

    async def _handle_payment_failed(self, entity: dict):
        sub_entity = entity.get("subscription", {}).get("entity", {})
        rz_sub_id = sub_entity.get("id") if sub_entity else None
        if not rz_sub_id:
            return

        async with get_serializable_transaction() as conn:
            sub = await conn.fetchrow(
                "SELECT id, user_id, payment_retry_count FROM user_subscriptions WHERE razorpay_subscription_id = $1 FOR UPDATE",
                rz_sub_id,
            )
            if not sub:
                return

            retry_count = (sub["payment_retry_count"] or 0) + 1
            now = datetime.now(timezone.utc)

            # After 3 retries, enter grace period
            new_status = "PAST_DUE"
            grace_end = None
            if retry_count >= 3:
                new_status = "GRACE_PERIOD"
                grace_end = now + timedelta(days=7)

            await conn.execute(
                """
                UPDATE user_subscriptions
                SET status = $1, payment_retry_count = $2, grace_period_end = $3, updated_at = $4
                WHERE id = $5
                """,
                new_status, retry_count, grace_end, now, sub["id"],
            )

            await cache_delete(f"sub_active:{sub['user_id']}")

        await emit_and_publish(DomainEvent(
            event_type=SUBSCRIPTION_PAYMENT_FAILED,
            payload={
                "user_id": str(sub["user_id"]),
                "retry_count": retry_count,
                "status": new_status,
            },
        ))

    async def _handle_cancelled(self, entity: dict):
        sub_entity = entity.get("subscription", {}).get("entity", {})
        rz_sub_id = sub_entity.get("id") if sub_entity else None
        if not rz_sub_id:
            return

        async with get_serializable_transaction() as conn:
            sub = await conn.fetchrow(
                "SELECT id, user_id FROM user_subscriptions WHERE razorpay_subscription_id = $1 FOR UPDATE",
                rz_sub_id,
            )
            if not sub:
                return

            now = datetime.now(timezone.utc)
            await conn.execute(
                """
                UPDATE user_subscriptions
                SET status = 'CANCELLED', cancelled_at = $1, ended_at = $1, updated_at = $1
                WHERE id = $2
                """,
                now, sub["id"],
            )

            await cache_delete(f"sub_active:{sub['user_id']}")

        await emit_and_publish(DomainEvent(
            event_type=SUBSCRIPTION_CANCELLED,
            payload={"user_id": str(sub["user_id"])},
        ))
