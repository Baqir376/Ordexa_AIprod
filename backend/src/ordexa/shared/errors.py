"""Explicit error categories (SRS 14.1) mapped to HTTP responses with a consistent body."""

from __future__ import annotations

from enum import StrEnum

from fastapi import Request
from fastapi.responses import JSONResponse


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TENANT_SCOPE_ERROR = "TENANT_SCOPE_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    LICENSE_LIMIT = "LICENSE_LIMIT"
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_STATUS = {
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.AUTHENTICATION_REQUIRED: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.TENANT_SCOPE_ERROR: 404,  # never reveal that another tenant's record exists
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.LICENSE_LIMIT: 402,
    ErrorCode.DUPLICATE_REQUEST: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL_ERROR: 500,
}


class OrdexaError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


async def ordexa_error_handler(request: Request, exc: OrdexaError) -> JSONResponse:
    return JSONResponse(
        status_code=_STATUS[exc.code],
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
                "correlation_id": request.headers.get("x-correlation-id"),
            }
        },
    )
