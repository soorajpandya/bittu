"""
Web Push (VAPID) delivery for QR-waitlist customer notifications.

Singleton VAPID keypair is auto-generated on first use and persisted to
`web_push_vapid_keys`. Subscriptions live in `web_push_subscriptions` keyed
by waitlist entry id.

A handler on `waitlist.updated` checks for status == 'notified' and pushes
"Your table is ready" to every subscription for that entry.
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

from app.core.database import get_connection
from app.core.events import DomainEvent, subscribe

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
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT public_key, private_pem FROM web_push_vapid_keys WHERE id = 1"
            )
            if not row:
                kp = _generate_keypair()
                await conn.execute(
                    """INSERT INTO web_push_vapid_keys (id, public_key, private_pem)
                       VALUES (1, $1, $2)
                       ON CONFLICT (id) DO NOTHING""",
                    kp["public_key"], kp["private_pem"],
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
    async with get_connection() as conn:
        await conn.execute(
            """INSERT INTO web_push_subscriptions
               (entry_id, restaurant_id, endpoint, p256dh, auth, user_agent)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (entry_id, endpoint) DO UPDATE
                 SET p256dh = EXCLUDED.p256dh,
                     auth = EXCLUDED.auth,
                     user_agent = EXCLUDED.user_agent""",
            entry_id, restaurant_id, endpoint, p256dh, auth, user_agent,
        )


async def _fetch_subscriptions(entry_id: str) -> list[dict]:
    async with get_connection() as conn:
        rows = await conn.fetch(
            "SELECT id, endpoint, p256dh, auth FROM web_push_subscriptions WHERE entry_id = $1",
            entry_id,
        )
    return [dict(r) for r in rows]


async def _delete_subscription(sub_id) -> None:
    async with get_connection() as conn:
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
        return 0

    subs = await _fetch_subscriptions(entry_id)
    if not subs:
        return 0

    keys = await get_vapid_keys()
    body = json.dumps(payload)
    delivered = 0

    def _blocking_send(sub: dict) -> tuple[bool, Optional[int]]:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=body,
                vapid_private_key=keys["private_pem"],
                vapid_claims={"sub": DEFAULT_VAPID_SUBJECT},
                ttl=ttl,
            )
            return True, None
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            return False, status
        except Exception:
            logger.exception("webpush_unexpected_error")
            return False, None

    for sub in subs:
        ok, status = await asyncio.to_thread(_blocking_send, sub)
        if ok:
            delivered += 1
        elif status in (404, 410):
            await _delete_subscription(sub["id"])
            logger.info("webpush_subscription_pruned", id=str(sub["id"]), status=status)
        else:
            logger.warning("webpush_delivery_failed", id=str(sub["id"]), status=status)
    return delivered


# ── Event wiring ─────────────────────────────────────────────

async def _on_waitlist_updated(event: DomainEvent) -> None:
    payload = event.payload or {}
    if payload.get("status") != "notified":
        return
    entry_id = payload.get("id")
    if not entry_id:
        return
    table_no = payload.get("assigned_table_number")
    msg_body = (
        f"Your table is ready — Table {table_no}. Please proceed to the host stand."
        if table_no
        else "You're up next! Please proceed to the host stand."
    )
    push_payload = {
        "title": "Your table is ready",
        "body": msg_body,
        "tag": f"waitlist-{entry_id}",
        "entry_id": entry_id,
    }
    try:
        n = await send_push(str(entry_id), push_payload)
        if n:
            logger.info("waitlist_push_sent", entry_id=str(entry_id), count=n)
    except Exception:
        logger.exception("waitlist_push_handler_error", entry_id=str(entry_id))


def register_push_handlers() -> None:
    """Idempotent: subscribe the waitlist push handler to the event bus."""
    subscribe("waitlist.updated", _on_waitlist_updated)
    logger.info("push_handlers_registered")
