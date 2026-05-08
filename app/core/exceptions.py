"""
Custom exception classes and centralized error handling.
"""
from fastapi import HTTPException, status


class AppException(HTTPException):
    """
    Base application exception.

    All subclasses carry:
      - error_code: machine-readable string (e.g. "NOT_FOUND")
      - retryable:  True if the caller can safely retry the request
    """
    def __init__(
        self,
        status_code: int,
        detail: str,
        error_code: str = "UNKNOWN",
        retryable: bool = False,
    ):
        super().__init__(status_code=status_code, detail=detail)
        self.error_code = error_code
        self.retryable = retryable


class NotFoundError(AppException):
    def __init__(self, resource: str, identifier: str = ""):
        detail = f"{resource} not found" + (f": {identifier}" if identifier else "")
        super().__init__(status_code=404, detail=detail, error_code="NOT_FOUND", retryable=False)


class ConflictError(AppException):
    def __init__(self, detail: str):
        super().__init__(status_code=409, detail=detail, error_code="CONFLICT", retryable=False)


class ForbiddenError(AppException):
    def __init__(self, detail: str = "Access denied"):
        super().__init__(status_code=403, detail=detail, error_code="FORBIDDEN", retryable=False)


class UnauthorizedError(AppException):
    def __init__(self, detail: str = "Authentication required"):
        super().__init__(status_code=401, detail=detail, error_code="UNAUTHORIZED", retryable=False)


class ValidationError(AppException):
    def __init__(self, detail: str):
        super().__init__(status_code=422, detail=detail, error_code="VALIDATION_ERROR", retryable=False)


class RateLimitError(AppException):
    def __init__(self):
        super().__init__(
            status_code=429,
            detail="Rate limit exceeded. Please try again later.",
            error_code="RATE_LIMIT_EXCEEDED",
            retryable=True,
        )


class PaymentError(AppException):
    def __init__(self, detail: str):
        super().__init__(status_code=402, detail=detail, error_code="PAYMENT_ERROR", retryable=False)


class InvalidStateTransition(AppException):
    def __init__(self, entity: str, from_state: str, to_state: str):
        super().__init__(
            status_code=409,
            detail=f"Invalid {entity} state transition: {from_state} → {to_state}",
            error_code="INVALID_STATE_TRANSITION",
            retryable=False,
        )


class LockAcquisitionError(AppException):
    def __init__(self, resource: str):
        super().__init__(
            status_code=409,
            detail=f"Resource is currently being modified: {resource}",
            error_code="LOCK_CONFLICT",
            retryable=True,  # caller should retry after a brief back-off
        )


class InventoryError(AppException):
    def __init__(self, detail: str):
        super().__init__(status_code=409, detail=detail, error_code="INVENTORY_ERROR", retryable=False)


class CheckoutError(AppException):
    """Raised when a checkout transaction fails for a business-logic reason."""
    def __init__(self, detail: str, error_code: str = "CHECKOUT_FAILED", retryable: bool = False):
        super().__init__(status_code=422, detail=detail, error_code=error_code, retryable=retryable)
