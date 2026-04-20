"""Python SDK — typed HTTP client for the canonical Musubi API.

Per [[07-interfaces/sdk]]. Both sync (:class:`MusubiClient`, backed by
``httpx.Client``) and async (:class:`AsyncMusubiClient`, backed by
``httpx.AsyncClient``) variants share the same resource namespaces +
typed exception hierarchy + :class:`SDKResult` wrapper for adapters
that prefer ``Result[T, E]`` over exceptions.

Adapters import from this package:

    from musubi.sdk import MusubiClient, AsyncMusubiClient
    from musubi.sdk import Forbidden, BackendUnavailable, RetryPolicy
    from musubi.sdk.testing import FakeMusubiClient

Note: the spec at [[07-interfaces/sdk]] still refers to the SDK as a
sibling package ``musubi-client``. ADR-0015 + ADR-0016 moved it to
``src/musubi/sdk/`` inside the monorepo; the spec is updated in-PR
with a ``spec-update:`` trailer when this slice lands.
"""

from musubi.sdk.async_client import AsyncMusubiClient
from musubi.sdk.client import MusubiClient
from musubi.sdk.exceptions import (
    BackendUnavailable,
    BadRequest,
    Conflict,
    Forbidden,
    InternalError,
    MusubiError,
    NetworkError,
    NotFound,
    RateLimited,
    Unauthorized,
)
from musubi.sdk.result import SDKResult
from musubi.sdk.retry import RetryPolicy

__all__ = [
    "AsyncMusubiClient",
    "BackendUnavailable",
    "BadRequest",
    "Conflict",
    "Forbidden",
    "InternalError",
    "MusubiClient",
    "MusubiError",
    "NetworkError",
    "NotFound",
    "RateLimited",
    "RetryPolicy",
    "SDKResult",
    "Unauthorized",
]
