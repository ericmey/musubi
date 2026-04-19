"""Typed error response shapes + ``Result[T, E]`` → HTTP mapping.

Per [[07-interfaces/canonical-api]] § Response shapes, every error has
the same JSON envelope::

    {"error": {"code": "...", "detail": "...", "hint": "..."}}

The ``code`` enum is fixed (BAD_REQUEST, UNAUTHORIZED, FORBIDDEN,
NOT_FOUND, CONFLICT, RATE_LIMITED, BACKEND_UNAVAILABLE, INTERNAL).
Adapters translate; they never re-invent.

Routers raise :class:`APIError` (or use the helpers below) instead of
FastAPI's ``HTTPException`` so the structured envelope is uniform; the
exception handler at app construction maps any raised ``APIError``
back to the JSON body.
"""

from __future__ import annotations

from typing import Literal

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

ErrorCode = Literal[
    "BAD_REQUEST",
    "UNAUTHORIZED",
    "FORBIDDEN",
    "NOT_FOUND",
    "CONFLICT",
    "RATE_LIMITED",
    "BACKEND_UNAVAILABLE",
    "INTERNAL",
]


class ErrorBody(BaseModel):
    code: ErrorCode
    detail: str
    hint: str = ""


class ErrorResponse(BaseModel):
    error: ErrorBody


class APIError(Exception):
    """Raised by routers to surface a typed HTTP error envelope."""

    def __init__(
        self,
        *,
        status_code: int,
        code: ErrorCode,
        detail: str,
        hint: str = "",
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.hint = hint


_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    429: "RATE_LIMITED",
    503: "BACKEND_UNAVAILABLE",
}


def error_response(
    status_code: int,
    detail: str,
    *,
    code: ErrorCode | None = None,
    hint: str = "",
) -> JSONResponse:
    """Build a typed error JSON response.

    Convenience for middleware that doesn't have an :class:`APIError` to
    raise (auth middleware does this directly because it runs outside
    the FastAPI exception-handler chain in some cases).
    """
    resolved = code or _STATUS_TO_CODE.get(status_code, "INTERNAL")
    body = ErrorResponse(
        error=ErrorBody(code=resolved, detail=detail, hint=hint),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())


async def api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """FastAPI exception handler — maps :class:`APIError` to the envelope."""
    if not isinstance(exc, APIError):
        # Defensive: anything else becomes a 500 with the message redacted.
        return error_response(
            status_code=500,
            detail="internal error",
            code="INTERNAL",
            hint="check server logs with the X-Request-Id header value",
        )
    return error_response(
        status_code=exc.status_code,
        detail=exc.detail,
        code=exc.code,
        hint=exc.hint,
    )


__all__ = [
    "APIError",
    "ErrorBody",
    "ErrorCode",
    "ErrorResponse",
    "api_error_handler",
    "error_response",
]
