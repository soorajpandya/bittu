from fastapi import Depends

from app.core.auth import UserContext, get_current_user
from app.core.exceptions import ForbiddenError
from app.schemas.rbac import PermissionDecision
from app.services.rbac_service import rbac_service


def require_permission(permission_key: str):
    async def _check(user: UserContext = Depends(get_current_user)) -> UserContext:
        decision: PermissionDecision = await rbac_service.check_permission(user, permission_key)
        if not decision.allowed:
            raise ForbiddenError(f"Permission denied: {permission_key}")

        # Attach contextual permission metadata for downstream business rules.
        user.permission_key = decision.permission_key
        user.permission_meta = decision.meta
        user.role_id = decision.role_id or user.role_id
        return user

    return _check
