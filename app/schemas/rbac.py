from pydantic import BaseModel, Field


class PermissionDecision(BaseModel):
    permission_key: str
    allowed: bool
    role_id: str | None = None
    role_name: str | None = None
    branch_id: str | None = None
    meta: dict = Field(default_factory=dict)


class ActivityLogCreate(BaseModel):
    user_id: str
    action: str
    entity_type: str
    entity_id: str | None = None
    branch_id: str | None = None
    metadata: dict = Field(default_factory=dict)
