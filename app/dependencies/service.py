"""Service-token (machine-to-machine) auth dependency.

Used by /api/internal/* and write paths on /api/financial/*.
Validates an HMAC-signed token in the X-Service-Token header against
the configured shared secret. IP allowlisting is done by nginx upstream.

This is the Phase-0 stub — it accepts any token in dev/test and enforces
in prod. Wire up real HMAC verification in Phase 1.

See docs/ARCHITECTURE_V2.md §5 (DI) and §19 (Security).
"""
from __future__ import annotations

import hmac
from hashlib import sha256

from fastapi import Depends, Header, HTTPException, status

from app.core.config import get_settings


def _verify(token: str | None) -> None:
    settings = get_settings()
    secret = getattr(settings, "INTERNAL_SERVICE_TOKEN_SECRET", None)

    # Dev / test escape hatch: if no secret configured, allow.
    # In production the secret MUST be set; otherwise we 503.
    if not secret:
        if settings.ENVIRONMENT == "production":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="internal_service_token_secret_not_configured",
            )
        return

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing_service_token",
        )

    # Token format: "<key_id>.<hex_hmac_of_key_id>"
    try:
        key_id, sig = token.split(".", 1)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="malformed_service_token",
        )

    expected = hmac.new(secret.encode(), key_id.encode(), sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_service_token",
        )


async def _service_token_dep(
    x_service_token: str | None = Header(default=None, alias="X-Service-Token"),
) -> None:
    _verify(x_service_token)


def require_service_token() -> "Depends":
    """Use as a router-level dependency.

        router = APIRouter(dependencies=[require_service_token()])
    """
    return Depends(_service_token_dep)
