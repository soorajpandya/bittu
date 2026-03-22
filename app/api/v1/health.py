"""Health & readiness endpoints."""
import os
from fastapi import APIRouter

from app.core.database import get_connection
from app.core.redis import get_redis
from app.realtime import manager as ws_manager

router = APIRouter(tags=["Health"])

_VERSION = os.environ.get("APP_VERSION", "1.0.0")
_BUILD_SHA = os.environ.get("BUILD_SHA", "dev")


@router.get("/health")
async def health():
    return {"status": "ok", "version": _VERSION, "build": _BUILD_SHA}


@router.get("/ready")
async def readiness():
    checks = {}
    # Postgres
    try:
        async with get_connection() as conn:
            await conn.fetchval("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = str(e)

    # Redis
    try:
        r = get_redis()
        await r.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = str(e)

    all_ok = all(v == "ok" for v in checks.values())
    checks["websocket_connections"] = ws_manager.stats
    return {"ready": all_ok, "checks": checks}
