"""
Web Push (VAPID) delivery for QR-waitlist customer notifications.

Singleton VAPID keypair is auto-generated on first use and persisted to
`web_push_vapid_keys`. Subscriptions live in `web_push_subscriptions` keyed
by waitlist entry id.

A transactional outbox records notifications; the maintenance worker retries
bounded Web Push delivery independently of HTTP requests.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Optional

import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.database import get_service_connection
from app.core.exceptions import ConflictError
from urllib.parse import urlsplit
from uuid import uuid4

logger = structlog.get_logger(__name__)

_cached_keys: Optional[dict] = None
_keys_lock = asyncio.Lock()

DEFAULT_VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@bittu.app")


# ── Key management ────────────────────────────────────────────


def _generate_keypair() -> dict:
    """Generate a P-256 keypair suitable for VAPID."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    nums = private_key.public_key().public_numbers()
    raw = b"\x04" + nums.x.to_bytes(32, "big") + nums.y.to_bytes(32, "big")
    public_b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return {"public_key": public_b64, "private_pem": private_pem}


async def get_vapid_keys() -> dict:
    """Lazily load (or generate + persist) the singleton VAPID keypair."""
    global _cached_keys
    if _cached_keys is not None:
        return _cached_keys
    async with _keys_lock:
        if _cached_keys is not None:
            return _cached_keys
        async with get_service_connection() as conn:
            row = await conn.fetchrow("SELECT public_key, private_pem FROM web_push_vapid_keys WHERE id = 1")
            if not row:
                kp = _generate_keypair()
                await conn.execute(
                    """INSERT INTO web_push_vapid_keys (id, public_key, private_pem)
                       VALUES (1, $1, $2)
                       ON CONFLICT (id) DO NOTHING""",
                    kp["public_key"],
                    kp["private_pem"],
                )
                row = await conn.fetchrow(
                    "SELECT public_key, private_pem FROM web_push_vapid_keys WHERE id = 1"
                )
                logger.info("vapid_keypair_generated", public_key_prefix=kp["public_key"][:16])
        _cached_keys = {"public_key": row["public_key"], "private_pem": row["private_pem"]}
        return _cached_keys


# ── Subscription storage ─────────────────────────────────────


async def save_subscription(
    entry_id: str,
    restaurant_id: str,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: Optional[str] = None,
) -> None:
    validate_subscription(endpoint, p256dh, auth)
    async with get_service_connection() as conn, conn.transaction():
        entry = await conn.fetchrow(
            "SELECT status FROM waitlist_entries WHERE id=$1::uuid FOR UPDATE", entry_id
        )
        if not entry or entry["status"] not in {"waiting", "notified"}:
            raise ConflictError("Waitlist entry is no longer active")
        count = await conn.fetchval(
            "SELECT count(*) FROM web_push_subscriptions WHERE entry_id=$1::uuid AND endpoint<>$2",
            entry_id,
            endpoint,
        )
        if count >= 3:
            raise ConflictError("At most three browsers can receive notifications for this entry")
        await conn.execute(
            """INSERT INTO web_push_subscriptions
               (entry_id, restaurant_id, endpoint, p256dh, auth, user_agent)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (entry_id, endpoint) DO UPDATE
                 SET p256dh = EXCLUDED.p256dh,
                     auth = EXCLUDED.auth,
                     user_agent = EXCLUDED.user_agent""",
            entry_id,
            restaurant_id,
            endpoint,
            p256dh,
            auth,
            (user_agent or "")[:512],
        )

        await conn.execute(
            "UPDATE waitlist_push_jobs SET state='pending',next_attempt_at=now(),attempts=0 WHERE entry_id=$1::uuid AND state IN ('pending','failed')",
            entry_id,
        )


async def _fetch_subscriptions(entry_id: str) -> list[dict]:
    async with get_service_connection() as conn:
        rows = await conn.fetch(
            "SELECT id, endpoint, p256dh, auth FROM web_push_subscriptions WHERE entry_id = $1",
            entry_id,
        )
    return [dict(r) for r in rows]


async def _delete_subscription(sub_id) -> None:
    async with get_service_connection() as conn:
        await conn.execute("DELETE FROM web_push_subscriptions WHERE id = $1", sub_id)


# ── Push delivery ────────────────────────────────────────────


async def send_push(entry_id: str, payload: dict, ttl: int = 60) -> int:
    """Push `payload` (will be JSON-encoded) to every saved subscription for
    the given waitlist entry. Returns the count of successful deliveries.
    Stale subscriptions (404/410) are auto-pruned."""
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.error("pywebpush_not_installed")
        raise RuntimeError("pywebpush is not installed")

    subs = await _fetch_subscriptions(entry_id)
    if not subs:
        return 0

    keys = await get_vapid_keys()
    body = json.dumps(payload)
    delivered = 0
    failed = False

    def _blocking_send(sub: dict) -> tuple[bool, Optional[int]]:
        try:
            validate_subscription(sub["endpoint"], sub["p256dh"], sub["auth"])
            import requests
            from py_vapid import Vapid

            class NoRedirectSession(requests.Session):
                def request(self, *args, **kwargs):
                    kwargs["allow_redirects"] = False
                    return super().request(*args, **kwargs)

            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=body,
                vapid_private_key=Vapid.from_pem(keys["private_pem"].encode()),
                vapid_claims={"sub": DEFAULT_VAPID_SUBJECT},
                ttl=ttl,
                timeout=10,
                requests_session=NoRedirectSession(),
            )
            return True, None
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            return False, status
        except Exception:
            logger.exception("webpush_unexpected_error")
            return False, None

    for sub in subs:
        async with get_service_connection() as conn:
            active = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM waitlist_entries WHERE id=$1::uuid AND status='notified' AND expires_at>now())",
                entry_id,
            )
        if not active:
            return delivered
        ok, status = await asyncio.to_thread(_blocking_send, sub)
        if ok:
            delivered += 1
        elif status in (404, 410):
            await _delete_subscription(sub["id"])
            logger.info("webpush_subscription_pruned", id=str(sub["id"]), status=status)
        else:
            failed = True
            logger.warning("webpush_delivery_failed", id=str(sub["id"]), status=status)
    if failed:
        raise RuntimeError("Push provider delivery failed")
    return delivered


# ── Event wiring ─────────────────────────────────────────────


def register_push_handlers() -> None:
    """Delivery is handled by the durable scheduler, never by request callbacks."""
    return None


def validate_subscription(endpoint, p256dh, auth):
    """Only supported Web Push providers; arbitrary URLs must never reach HTTP."""
    try:
        url = urlsplit(endpoint)
        host = (url.hostname or "").lower()
        supported = host in {
            "fcm.googleapis.com",
            "updates.push.services.mozilla.com",
            "web.push.apple.com",
        } or host.endswith(".push.apple.com")
        if (
            not supported
            or url.scheme != "https"
            or url.port not in (None, 443)
            or url.username
            or url.password
            or url.fragment
        ):
            raise ValueError("Unsupported push endpoint")

        def decode(value):
            return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)

        point, secret = decode(p256dh), decode(auth)
        if len(point) != 65 or point[0] != 4 or len(secret) != 16:
            raise ValueError("Invalid push keys")
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except (ValueError, TypeError) as exc:
        raise ValueError("A supported HTTPS push endpoint and valid P-256/auth keys are required") from exc


async def run_delivery_tick():
    """Claim a bounded batch atomically; reclaim abandoned leases after a crash."""
    token = uuid4()
    async with get_service_connection() as conn:
        await conn.execute(
            "UPDATE waitlist_push_jobs j SET state='cancelled',lease_token=NULL FROM waitlist_entries e WHERE j.entry_id=e.id AND j.state IN ('pending','sending') AND (e.status<>'notified' OR e.expires_at<=now())"
        )
        await conn.execute(
            "UPDATE waitlist_push_jobs SET state='failed',lease_token=NULL,last_error='Delivery lease expired' WHERE state='sending' AND lease_until<now() AND attempts>=5"
        )
        jobs = await conn.fetch(
            "WITH due AS (SELECT j.entry_id FROM waitlist_push_jobs j JOIN waitlist_entries e ON e.id=j.entry_id "
            "WHERE e.status='notified' AND e.expires_at>now() AND j.attempts<5 AND ((j.state='pending' AND j.next_attempt_at<=now()) "
            "OR (j.state='sending' AND j.lease_until<now())) ORDER BY j.next_attempt_at LIMIT 4 FOR UPDATE OF j SKIP LOCKED) "
            "UPDATE waitlist_push_jobs j SET state='sending',attempts=attempts+1,lease_until=now()+interval '90 seconds',lease_token=$1 "
            "FROM due WHERE j.entry_id=due.entry_id RETURNING j.entry_id,j.attempts",
            token,
        )

    async def deliver(job):
        entry_id = str(job["entry_id"])
        delivered, error = 0, None
        try:
            async with get_service_connection() as conn:
                entry = await conn.fetchrow(
                    "SELECT restaurant_id,branch_id,expires_at FROM waitlist_entries WHERE id=$1::uuid AND status='notified' AND expires_at>now()",
                    entry_id,
                )
            if not entry:
                return
            from datetime import datetime, timezone

            ttl = max(1, int((entry["expires_at"] - datetime.now(timezone.utc)).total_seconds()))
            url = f"/q/{entry['restaurant_id']}?entry_id={entry_id}"
            if entry["branch_id"]:
                url += f"&branch_id={entry['branch_id']}"
            delivered = await send_push(
                entry_id,
                {
                    "title": "Your table is ready",
                    "body": "Please proceed to the host stand.",
                    "tag": f"waitlist-{entry_id}",
                    "entry_id": entry_id,
                    "url": url,
                },
                ttl=min(ttl, 60),
            )
            if not delivered:
                error = "No active browser subscriptions"
        except Exception:
            error = "Push provider unavailable"
            logger.exception("waitlist_push_job_failed", entry_id=entry_id)
        state = "delivered" if delivered else "failed" if job["attempts"] >= 5 else "pending"
        async with get_service_connection() as conn:
            await conn.execute(
                "UPDATE waitlist_push_jobs SET state=$3,last_error=$4,lease_until=NULL,lease_token=NULL,next_attempt_at=now()+$5::interval,delivered_at=CASE WHEN $3='delivered' THEN now() ELSE delivered_at END WHERE entry_id=$1::uuid AND lease_token=$2",
                entry_id,
                token,
                state,
                error,
                __import__("datetime").timedelta(seconds=min(120, 10 * 2 ** job["attempts"])),
            )

    await asyncio.gather(*(deliver(job) for job in jobs))
