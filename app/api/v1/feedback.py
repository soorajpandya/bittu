"""Feedback Management endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_role
from app.services.feedback_service import FeedbackService

router = APIRouter(prefix="/feedback", tags=["Feedback"])
_svc = FeedbackService()


class FeedbackCreate(BaseModel):
    customer_id: Optional[int] = None
    order_id: Optional[str] = None
    rating: int = Field(ge=1, le=5)
    food_rating: Optional[int] = Field(None, ge=1, le=5)
    service_rating: Optional[int] = Field(None, ge=1, le=5)
    ambience_rating: Optional[int] = Field(None, ge=1, le=5)
    comment: Optional[str] = None
    source: Optional[str] = "pos"


class FeedbackRespond(BaseModel):
    staff_response: str


@router.get("")
async def list_feedback(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.list_feedback(user, limit=limit, offset=offset)


@router.get("/{feedback_id}")
async def get_feedback(
    feedback_id: int,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.get_feedback(user, feedback_id)


@router.post("", status_code=201)
async def create_feedback(
    body: FeedbackCreate,
    user: UserContext = Depends(require_role("owner", "manager", "cashier", "waiter")),
):
    return await _svc.create_feedback(user, body.model_dump())


@router.patch("/{feedback_id}/respond")
async def respond_to_feedback(
    feedback_id: int,
    body: FeedbackRespond,
    user: UserContext = Depends(require_role("owner", "manager")),
):
    return await _svc.respond_to_feedback(user, feedback_id, body.staff_response)


@router.delete("/{feedback_id}")
async def delete_feedback(
    feedback_id: int,
    user: UserContext = Depends(require_role("owner")),
):
    return await _svc.delete_feedback(user, feedback_id)
