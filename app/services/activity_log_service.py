from typing import Any

from app.core.database import get_connection
from app.core.logging import get_logger
from app.schemas.rbac import ActivityLogCreate

logger = get_logger(__name__)


async def log_activity(
    user_id: str,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    branch_id: str | None = None,
) -> None:
    payload = ActivityLogCreate(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata=metadata or {},
        branch_id=branch_id,
    )

    try:
        async with get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO activity_logs (user_id, branch_id, action, entity_type, entity_id, metadata)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                payload.user_id,
                payload.branch_id,
                payload.action,
                payload.entity_type,
                payload.entity_id,
                payload.metadata,
            )
    except Exception as exc:
        # Authorization/business flows must continue even if log persistence fails.
        logger.warning(
            "activity_log_write_failed",
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            error=str(exc),
        )
