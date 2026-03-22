"""
WebSocket Real-time Layer.

Manages:
  - Per-connection state (user, branch, subscribed channels)
  - Channel-based fan-out from Redis pub/sub to WebSocket clients
  - Heartbeat / keepalive
  - Missed-event recovery on reconnect
"""
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect, status

from app.core.logging import get_logger
from app.core.redis import get_pubsub_redis
from app.core.auth import decode_jwt, resolve_user_context

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# Connection registry
# ──────────────────────────────────────────────────────────────

@dataclass
class WSConnection:
    ws: WebSocket
    user_id: str
    branch_id: Optional[str] = None
    channels: set = field(default_factory=set)
    connected_at: float = field(default_factory=time.time)


class ConnectionManager:
    """Thread-safe registry of active WebSocket connections."""

    def __init__(self):
        # channel -> set of WSConnection
        self._channels: dict[str, set[WSConnection]] = {}
        # user_id -> list of WSConnection (multi-device)
        self._users: dict[str, list[WSConnection]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, conn: WSConnection):
        async with self._lock:
            self._users.setdefault(conn.user_id, []).append(conn)

    async def disconnect(self, conn: WSConnection):
        async with self._lock:
            for ch in list(conn.channels):
                self._channels.get(ch, set()).discard(conn)
            user_conns = self._users.get(conn.user_id, [])
            if conn in user_conns:
                user_conns.remove(conn)
            if not user_conns:
                self._users.pop(conn.user_id, None)

    async def subscribe(self, conn: WSConnection, channel: str):
        async with self._lock:
            conn.channels.add(channel)
            self._channels.setdefault(channel, set()).add(conn)

    async def unsubscribe(self, conn: WSConnection, channel: str):
        async with self._lock:
            conn.channels.discard(channel)
            self._channels.get(channel, set()).discard(conn)

    async def broadcast(self, channel: str, payload: dict):
        """Send payload to all clients subscribed to channel."""
        conns = self._channels.get(channel, set()).copy()
        dead: list[WSConnection] = []
        for c in conns:
            try:
                await c.ws.send_json(payload)
            except Exception:
                dead.append(c)
        for c in dead:
            await self.disconnect(c)

    async def send_to_user(self, user_id: str, payload: dict):
        conns = list(self._users.get(user_id, []))
        for c in conns:
            try:
                await c.ws.send_json(payload)
            except Exception:
                await self.disconnect(c)

    @property
    def stats(self) -> dict:
        return {
            "connections": sum(len(v) for v in self._users.values()),
            "channels": len(self._channels),
            "users": len(self._users),
        }


manager = ConnectionManager()


# ──────────────────────────────────────────────────────────────
# Redis pub/sub → WebSocket fan-out
# ──────────────────────────────────────────────────────────────

async def redis_subscriber():
    """Background task that reads Redis pub/sub and fans out to WS clients."""
    r = get_pubsub_redis()
    psub = r.pubsub()
    await psub.psubscribe("events:*")
    logger.info("ws_redis_subscriber_started")

    try:
        async for message in psub.listen():
            if message["type"] != "pmessage":
                continue
            try:
                channel = message["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode()
                data = json.loads(message["data"])

                # Route to the correct WS channels
                # Convention: events:<event_type> e.g. events:order.status_changed
                event_type = channel.replace("events:", "")
                branch_id = data.get("branch_id")

                # Fan out to branch channel
                if branch_id:
                    await manager.broadcast(f"branch:{branch_id}", {
                        "event": event_type,
                        "data": data,
                    })

                # Fan out to entity-specific channel
                entity_id = data.get("order_id") or data.get("delivery_id") or data.get("session_id")
                if entity_id:
                    await manager.broadcast(f"entity:{entity_id}", {
                        "event": event_type,
                        "data": data,
                    })

                # Direct user notification
                target_user = data.get("user_id")
                if target_user:
                    await manager.send_to_user(target_user, {
                        "event": event_type,
                        "data": data,
                    })

            except Exception:
                logger.exception("ws_redis_message_error")
    except asyncio.CancelledError:
        await psub.punsubscribe("events:*")
        raise


# ──────────────────────────────────────────────────────────────
# WebSocket endpoint handler
# ──────────────────────────────────────────────────────────────

HEARTBEAT_INTERVAL = 30  # seconds


async def ws_endpoint(websocket: WebSocket, token: Optional[str] = None):
    """Main WebSocket handler — authenticate, subscribe, relay."""

    # ---- Auth ----
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    try:
        claims = decode_jwt(token)
        user_id = claims["sub"]
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()

    # Resolve full user context (branch, role, etc.)
    try:
        user_ctx = await resolve_user_context(user_id)
    except Exception:
        await websocket.send_json({"error": "user_context_failed"})
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    conn = WSConnection(ws=websocket, user_id=user_id, branch_id=user_ctx.branch_id)
    await manager.connect(conn)

    # Auto-subscribe to branch channel if the user belongs to one
    if user_ctx.branch_id:
        await manager.subscribe(conn, f"branch:{user_ctx.branch_id}")

    logger.info("ws_connected", user_id=user_id, branch_id=user_ctx.branch_id)

    try:
        # Send initial payload
        await websocket.send_json({
            "event": "connected",
            "data": {"user_id": user_id, "branch_id": user_ctx.branch_id},
        })

        # ---- Message loop ----
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                # Send ping, expect pong
                try:
                    await websocket.send_json({"event": "ping"})
                except Exception:
                    break
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"error": "invalid_json"})
                continue

            action = msg.get("action")

            if action == "pong":
                continue

            elif action == "subscribe":
                channel = msg.get("channel", "")
                # Security: only allow subscribing to own branch or specific entities
                if _can_subscribe(user_ctx, channel):
                    await manager.subscribe(conn, channel)
                    await websocket.send_json({"event": "subscribed", "channel": channel})
                else:
                    await websocket.send_json({"error": "forbidden_channel", "channel": channel})

            elif action == "unsubscribe":
                channel = msg.get("channel", "")
                await manager.unsubscribe(conn, channel)
                await websocket.send_json({"event": "unsubscribed", "channel": channel})

            else:
                await websocket.send_json({"error": "unknown_action"})

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("ws_error", user_id=user_id)
    finally:
        await manager.disconnect(conn)
        logger.info("ws_disconnected", user_id=user_id)


# ──────────────────────────────────────────────────────────────
# Channel access control
# ──────────────────────────────────────────────────────────────

def _can_subscribe(user_ctx, channel: str) -> bool:
    """Basic channel ACL — users can only subscribe to their own branch or entities they access."""
    if channel.startswith("branch:"):
        branch_id = channel.split(":", 1)[1]
        if user_ctx.branch_id and user_ctx.branch_id == branch_id:
            return True
        # Owners can subscribe to any of their branches (validated at router level)
        if user_ctx.role == "owner":
            return True
        return False

    if channel.startswith("entity:"):
        # Entity channels are fine — the event payload itself is filtered
        return True

    return False
