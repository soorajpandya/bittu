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

    # ── Subscribe (create Razorpay subscription) ──

    async def subscribe(self, user: UserContext, plan_slug: str) -> dict:
        """
        Create a Razorpay subscription for the user.
        Returns Razorpay subscription details (id, short_url) for frontend checkout.
        """
        from app.services.razorpay_extended_service import RazorpayExtendedService
        rz = RazorpayExtendedService()

        async with get_serializable_transaction() as conn:
            # Validate plan
            plan = await conn.fetchrow(
                "SELECT * FROM subscription_plans WHERE slug = $1 AND is_active = true",
                plan_slug,
            )
            if not plan:
                raise NotFoundError("Plan", plan_slug)

            if not plan["razorpay_plan_id"]:
                raise ValidationError("This plan is not configured for payment yet. Contact support.")

            # Check existing active subscription
            existing = await conn.fetchrow(
                """
                SELECT id, status, plan_id FROM user_subscriptions
                WHERE user_id = $1 AND status IN ('ACTIVE', 'trialing', 'TRIAL', 'PENDING')
                ORDER BY created_at DESC LIMIT 1
                """,
                user.user_id,
            )
            if existing and existing["status"] in ("ACTIVE",):
                raise ValidationError("You already have an active subscription. Use upgrade instead.")

            # Create Razorpay subscription
            rz_sub = await rz.create_subscription(
                plan_id=plan["razorpay_plan_id"],
                total_count=12,  # 12 cycles for yearly
                user_id=user.user_id,
                plan_name=plan["name"],
                user_email=user.email or "",
            )

            now = datetime.now(timezone.utc)

            if existing:
                await conn.execute(
                    """
                    UPDATE user_subscriptions
                    SET plan_id = $1, status = 'PENDING',
                        razorpay_subscription_id = $2, updated_at = $3
                    WHERE id = $4
                    """,
                    plan["id"], rz_sub["id"], now, existing["id"],
                )
                sub_id = existing["id"]
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO user_subscriptions
                        (user_id, plan_id, status, razorpay_subscription_id, created_at, updated_at)
                    VALUES ($1, $2, 'PENDING', $3, $4, $4)
                    RETURNING id
                    """,
                    user.user_id, plan["id"], rz_sub["id"], now,
                )
                sub_id = row["id"]

            await cache_delete(f"sub_active:{user.user_id}")

        return {
            "subscription_id": sub_id,
            "razorpay_subscription_id": rz_sub["id"],
            "short_url": rz_sub.get("short_url", ""),
            "status": "PENDING",
            "plan": dict(plan),
        }

    # ── Cancel Subscription ──

    async def cancel_subscription(self, user: UserContext) -> dict:
        """
        Cancel the user's active subscription.
        Access continues until current period ends.
        """
        from app.services.razorpay_extended_service import RazorpayExtendedService
        rz = RazorpayExtendedService()

        async with get_serializable_transaction() as conn:
            sub = await conn.fetchrow(
                """
                SELECT id, status, razorpay_subscription_id, current_period_end
                FROM user_subscriptions
                WHERE user_id = $1 AND status IN ('ACTIVE', 'PAST_DUE', 'GRACE_PERIOD', 'trialing', 'TRIAL')
                ORDER BY created_at DESC LIMIT 1
                FOR UPDATE
                """,
                user.user_id,
            )
            if not sub:
                raise NotFoundError("Subscription", "No active subscription found")

            now = datetime.now(timezone.utc)

            # Cancel on Razorpay if exists
            if sub["razorpay_subscription_id"]:
                try:
                    import httpx, base64
                    settings = get_settings()
                    creds = base64.b64encode(
                        f"{settings.RAZORPAY_KEY_ID}:{settings.RAZORPAY_KEY_SECRET}".encode()
                    ).decode()
                    async with httpx.AsyncClient(timeout=30) as client:
                        await client.post(
                            f"https://api.razorpay.com/v1/subscriptions/{sub['razorpay_subscription_id']}/cancel",
                            json={"cancel_at_cycle_end": 1},
                            headers={
                                "Content-Type": "application/json",
                                "Authorization": f"Basic {creds}",
                            },
                        )
                except Exception as e:
                    logger.error("razorpay_cancel_failed", error=str(e))

            # If trial, cancel immediately; if paid, end at period end
            if sub["status"] in ("trialing", "TRIAL"):
                await conn.execute(
                    """
                    UPDATE user_subscriptions
                    SET status = 'CANCELLED', cancelled_at = $1, ended_at = $1, updated_at = $1
                    WHERE id = $2
                    """,
                    now, sub["id"],
                )
                end_date = now
            else:
                end_date = sub.get("current_period_end") or now
                await conn.execute(
                    """
                    UPDATE user_subscriptions
                    SET status = 'CANCELLED', cancelled_at = $1, ended_at = $2, updated_at = $1
                    WHERE id = $3
                    """,
                    now, end_date, sub["id"],
                )

            await cache_delete(f"sub_active:{user.user_id}")

        return {
            "status": "CANCELLED",
            "cancelled_at": now.isoformat(),
            "access_until": end_date.isoformat() if end_date else now.isoformat(),
        }

    # ── Upgrade Plan ──

    async def upgrade_plan(self, user: UserContext, new_plan_slug: str) -> dict:
        """
        Upgrade to a higher plan. Takes effect immediately.
        Creates a new Razorpay subscription for the new plan.
        """
        from app.services.razorpay_extended_service import RazorpayExtendedService
        rz = RazorpayExtendedService()

        async with get_serializable_transaction() as conn:
            # Get current subscription
            sub = await conn.fetchrow(
                """
                SELECT us.id, us.status, us.plan_id, us.razorpay_subscription_id,
                       sp.slug as current_slug, sp.price as current_price
                FROM user_subscriptions us
                LEFT JOIN subscription_plans sp ON sp.id = us.plan_id
                WHERE us.user_id = $1 AND us.status IN ('ACTIVE', 'trialing', 'TRIAL')
                ORDER BY us.created_at DESC LIMIT 1
                FOR UPDATE
                """,
                user.user_id,
            )
            if not sub:
                raise NotFoundError("Subscription", "No active subscription to upgrade")

            # Get new plan
            new_plan = await conn.fetchrow(
                "SELECT * FROM subscription_plans WHERE slug = $1 AND is_active = true",
                new_plan_slug,
            )
            if not new_plan:
                raise NotFoundError("Plan", new_plan_slug)

            if not new_plan["razorpay_plan_id"]:
                raise ValidationError("Target plan is not configured for payment yet.")

            # Ensure it's actually an upgrade
            if new_plan["price"] <= (sub["current_price"] or 0):
                raise ValidationError("Use downgrade endpoint for lower plans")

            # Cancel old Razorpay subscription
            if sub["razorpay_subscription_id"]:
                try:
                    import httpx, base64
                    settings = get_settings()
                    creds = base64.b64encode(
                        f"{settings.RAZORPAY_KEY_ID}:{settings.RAZORPAY_KEY_SECRET}".encode()
                    ).decode()
                    async with httpx.AsyncClient(timeout=30) as client:
                        await client.post(
                            f"https://api.razorpay.com/v1/subscriptions/{sub['razorpay_subscription_id']}/cancel",
                            json={"cancel_at_cycle_end": 0},
                            headers={
                                "Content-Type": "application/json",
                                "Authorization": f"Basic {creds}",
                            },
                        )
                except Exception as e:
                    logger.warning("razorpay_cancel_old_sub", error=str(e))

            # Create new Razorpay subscription for upgraded plan
            rz_sub = await rz.create_subscription(
                plan_id=new_plan["razorpay_plan_id"],
                total_count=12,
                user_id=user.user_id,
                plan_name=new_plan["name"],
                user_email=user.email or "",
            )

            now = datetime.now(timezone.utc)
            await conn.execute(
                """
                UPDATE user_subscriptions
                SET plan_id = $1, status = 'PENDING',
                    razorpay_subscription_id = $2,
                    upgrade_from_plan_id = $3, updated_at = $4
                WHERE id = $5
                """,
                new_plan["id"], rz_sub["id"], sub["plan_id"], now, sub["id"],
            )

            await cache_delete(f"sub_active:{user.user_id}")

        return {
            "subscription_id": sub["id"],
            "razorpay_subscription_id": rz_sub["id"],
            "short_url": rz_sub.get("short_url", ""),
            "status": "PENDING",
            "upgraded_from": sub["current_slug"],
            "upgraded_to": new_plan_slug,
            "plan": dict(new_plan),
        }

    # ── Downgrade Plan ──

    async def downgrade_plan(self, user: UserContext, new_plan_slug: str) -> dict:
        """
        Schedule a downgrade to a lower plan.
        Takes effect at next billing cycle.
        """
        async with get_serializable_transaction() as conn:
            sub = await conn.fetchrow(
                """
                SELECT us.id, us.status, us.plan_id, us.current_period_end,
                       sp.slug as current_slug, sp.price as current_price
                FROM user_subscriptions us
                LEFT JOIN subscription_plans sp ON sp.id = us.plan_id
                WHERE us.user_id = $1 AND us.status = 'ACTIVE'
                ORDER BY us.created_at DESC LIMIT 1
                FOR UPDATE
                """,
                user.user_id,
            )
            if not sub:
                raise NotFoundError("Subscription", "No active subscription to downgrade")

            new_plan = await conn.fetchrow(
                "SELECT * FROM subscription_plans WHERE slug = $1 AND is_active = true",
                new_plan_slug,
            )
            if not new_plan:
                raise NotFoundError("Plan", new_plan_slug)

            if new_plan["price"] >= (sub["current_price"] or 0):
                raise ValidationError("Use upgrade endpoint for higher plans")

            effective_date = sub.get("current_period_end") or (
                datetime.now(timezone.utc) + timedelta(days=30)
            )

            now = datetime.now(timezone.utc)
            await conn.execute(
                """
                UPDATE user_subscriptions
                SET downgrade_to_plan_id = $1, downgrade_effective_at = $2, updated_at = $3
                WHERE id = $4
                """,
                new_plan["id"], effective_date, now, sub["id"],
            )

        return {
            "status": "DOWNGRADE_SCHEDULED",
            "current_plan": sub["current_slug"],
            "downgrade_to": new_plan_slug,
            "effective_at": effective_date.isoformat(),
        }

    # ── Add-ons ──

    async def list_addons(self) -> list[dict]:
        """List all available add-on products."""
        async with get_connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM addon_products WHERE is_active = true ORDER BY name"
            )
            return [dict(r) for r in rows]

    async def purchase_addon(self, user: UserContext, addon_slug: str, quantity: int = 1, shipping_address: dict = None) -> dict:
        """Create an order for an add-on product (e.g., printer)."""
        from app.services.razorpay_extended_service import RazorpayExtendedService

        async with get_serializable_transaction() as conn:
            addon = await conn.fetchrow(
                "SELECT * FROM addon_products WHERE slug = $1 AND is_active = true",
                addon_slug,
            )
            if not addon:
                raise NotFoundError("AddOn", addon_slug)

            amount = float(addon["price"]) * quantity

            # Create Razorpay order for one-time payment
            rz = RazorpayExtendedService()
            import httpx, base64
            settings = get_settings()
            creds = base64.b64encode(
                f"{settings.RAZORPAY_KEY_ID}:{settings.RAZORPAY_KEY_SECRET}".encode()
            ).decode()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.razorpay.com/v1/orders",
                    json={
                        "amount": int(amount * 100),  # paise
                        "currency": "INR",
                        "notes": {
                            "user_id": user.user_id,
                            "addon_slug": addon_slug,
                            "quantity": str(quantity),
                        },
                    },
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Basic {creds}",
                    },
                )
                resp.raise_for_status()
                rz_order = resp.json()

            import json as _json
            row = await conn.fetchrow(
                """
                INSERT INTO addon_orders
                    (user_id, addon_id, quantity, amount, status, razorpay_order_id, shipping_address, created_at, updated_at)
                VALUES ($1, $2, $3, $4, 'pending', $5, $6, now(), now())
                RETURNING id
                """,
                user.user_id, addon["id"], quantity, amount,
                rz_order["id"],
                _json.dumps(shipping_address) if shipping_address else None,
            )

        return {
            "order_id": row["id"],
            "razorpay_order_id": rz_order["id"],
            "amount": amount,
            "currency": "INR",
            "addon": dict(addon),
        }

    # ── Admin Methods ──

    async def admin_list_subscriptions(self, status_filter: str = None, limit: int = 50, offset: int = 0) -> list[dict]:
        """Admin: List all subscriptions with optional status filter."""
        async with get_connection() as conn:
            if status_filter:
                rows = await conn.fetch(
                    """
                    SELECT us.*, sp.name as plan_name, sp.slug as plan_slug, sp.price as plan_price
                    FROM user_subscriptions us
                    LEFT JOIN subscription_plans sp ON sp.id = us.plan_id
                    WHERE us.status = $1
                    ORDER BY us.created_at DESC
                    LIMIT $2 OFFSET $3
                    """,
                    status_filter, limit, offset,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT us.*, sp.name as plan_name, sp.slug as plan_slug, sp.price as plan_price
                    FROM user_subscriptions us
                    LEFT JOIN subscription_plans sp ON sp.id = us.plan_id
                    ORDER BY us.created_at DESC
                    LIMIT $1 OFFSET $2
                    """,
                    limit, offset,
                )
            return [dict(r) for r in rows]

    async def admin_update_plan(self, plan_id: int, data: dict) -> dict:
        """Admin: Update plan pricing/features."""
        async with get_serializable_transaction() as conn:
            plan = await conn.fetchrow(
                "SELECT * FROM subscription_plans WHERE id = $1 FOR UPDATE", plan_id
            )
            if not plan:
                raise NotFoundError("Plan", str(plan_id))

            fields = {k: v for k, v in data.items() if v is not None}
            if not fields:
                return dict(plan)

            set_parts = []
            vals = [plan_id]
            for k, v in fields.items():
                vals.append(v)
                set_parts.append(f"{k} = ${len(vals)}")
            vals.append(datetime.now(timezone.utc))
            set_parts.append(f"updated_at = ${len(vals)}")

            row = await conn.fetchrow(
                f"UPDATE subscription_plans SET {', '.join(set_parts)} WHERE id = $1 RETURNING *",
                *vals,
            )
            return dict(row)
