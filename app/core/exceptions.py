"""
Custom exception classes and centralized error handling.
"""
from fastapi import HTTPException, status


class AppException(HTTPException):
    """Base application exception."""
    def __init__(self, status_code: int, detail: str, error_code: str = "UNKNOWN"):
        super().__init__(status_code=status_code, detail=detail)
        self.error_code = error_code


class NotFoundError(AppException):
    def __init__(self, resource: str, identifier: str = ""):
        detail = f"{resource} not found" + (f": {identifier}" if identifier else "")
        super().__init__(status_code=404, detail=detail, error_code="NOT_FOUND")


class ConflictError(AppException):
    def __init__(self, detail: str):
        super().__init__(status_code=409, detail=detail, error_code="CONFLICT")


class ForbiddenError(AppException):
    def __init__(self, detail: str = "Access denied"):
        super().__init__(status_code=403, detail=detail, error_code="FORBIDDEN")


class UnauthorizedError(AppException):
    def __init__(self, detail: str = "Authentication required"):
        super().__init__(status_code=401, detail=detail, error_code="UNAUTHORIZED")


class ValidationError(AppException):
    def __init__(self, detail: str):
        super().__init__(status_code=422, detail=detail, error_code="VALIDATION_ERROR")


class RateLimitError(AppException):
    def __init__(self):
        super().__init__(
            status_code=429,
            detail="Rate limit exceeded. Please try again later.",
            error_code="RATE_LIMIT_EXCEEDED",
        )


class PaymentError(AppException):
    def __init__(self, detail: str):
        super().__init__(status_code=402, detail=detail, error_code="PAYMENT_ERROR")


class InvalidStateTransition(AppException):
    def __init__(self, entity: str, from_state: str, to_state: str):
        super().__init__(
            status_code=409,
            detail=f"Invalid {entity} state transition: {from_state} → {to_state}",
            error_code="INVALID_STATE_TRANSITION",
        )


class LockAcquisitionError(AppException):
    def __init__(self, resource: str):
        super().__init__(
            status_code=409,
            detail=f"Resource is currently being modified: {resource}",
            error_code="LOCK_CONFLICT",
        )


class InventoryError(AppException):
    def __init__(self, detail: str):
        super().__init__(status_code=409, detail=detail, error_code="INVENTORY_ERROR")
