"""Typed exception hierarchy mirroring the canonical API's error envelope.

Per [[07-interfaces/canonical-api]] § Response shapes, every API error
returns ``{"error": {"code", "detail", "hint"}}``. The SDK maps each
``code`` to a typed Python exception so adapters can ``except
Forbidden:`` etc. without parsing the body.

The hierarchy:

- :class:`MusubiError` — base class for every error this SDK raises.
  Carries ``code``, ``detail``, ``hint``, ``status_code`` (when
  applicable, ``None`` for low-level network errors).
- :class:`BadRequest` (400), :class:`Unauthorized` (401),
  :class:`Forbidden` (403), :class:`NotFound` (404),
  :class:`Conflict` (409), :class:`RateLimited` (429),
  :class:`BackendUnavailable` (503), :class:`InternalError` (500) —
  one per HTTP status the spec enumerates.
- :class:`NetworkError` — DNS / connect / read failures from
  ``httpx`` that never reached an HTTP status.
"""

from __future__ import annotations


class MusubiError(Exception):
    """Base class for every error the SDK raises."""

    def __init__(
        self,
        *,
        code: str,
        detail: str,
        hint: str = "",
        status_code: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.hint = hint
        self.status_code = status_code


class BadRequest(MusubiError):
    pass


class Unauthorized(MusubiError):
    pass


class Forbidden(MusubiError):
    pass


class NotFound(MusubiError):
    pass


class Conflict(MusubiError):
    pass


class RateLimited(MusubiError):
    pass


class BackendUnavailable(MusubiError):
    pass


class InternalError(MusubiError):
    pass


class NetworkError(MusubiError):
    """DNS / connect / read errors that never reached an HTTP status."""


_STATUS_TO_EXCEPTION: dict[int, type[MusubiError]] = {
    400: BadRequest,
    401: Unauthorized,
    403: Forbidden,
    404: NotFound,
    409: Conflict,
    429: RateLimited,
    500: InternalError,
    503: BackendUnavailable,
}


def exception_for_status(status_code: int) -> type[MusubiError]:
    """Return the typed-exception class for an HTTP status code; falls
    back to :class:`MusubiError` for anything not enumerated."""
    return _STATUS_TO_EXCEPTION.get(status_code, MusubiError)


__all__ = [
    "BackendUnavailable",
    "BadRequest",
    "Conflict",
    "Forbidden",
    "InternalError",
    "MusubiError",
    "NetworkError",
    "NotFound",
    "RateLimited",
    "Unauthorized",
    "exception_for_status",
]
