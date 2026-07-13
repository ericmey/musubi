"""Routed post-authz idempotency dependency (Phase B).

Built for the body-derived capture routes. It EXPLICITLY ``Depends`` on the route's
:class:`~musubi.api.write_auth.AuthorizedWrite` dependency, so authentication + namespace authz
have ALREADY run before any cache lookup — replay can never happen pre-auth (SEC-002).

For an eligible request (carrying an ``Idempotency-Key``) it builds the principal-bound identity
and the byte-exact canonical digest, then drives :class:`IdempotencyLeaseCache.acquire`:

  - hit      → raise :class:`Replay` (the exception handler serves the cached response; the
               handler is NOT executed);
  - conflict → 409 (same key, different body); handler NOT executed;
  - in_flight→ 409, a VISIBLE conflict (no busy loop, no second execution); handler NOT executed;
  - acquired → establish the observer-visible lease state and return; the store-only observer
               completes + releases the lease. If anything fails between acquire and establishing
               that state, the lease is released here so it can never leak.

The completed-response store/replay bytes are owned by the observer (next commit).
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Request

from musubi.api.errors import APIError
from musubi.api.idempotency import IdempotencyLeaseCache, get_idempotency_lease_cache
from musubi.api.write_auth import AuthorizedWrite

_DIGEST_DOMAIN = b"musubi-idem-json-v1"

Identity = tuple[Any, ...]


@dataclass(frozen=True)
class IdempotentContext:
    """Handed to the capture handler. Carries the :class:`AuthorizedWrite`, plus — when the request
    is idempotent — the lease ``identity`` and ``owner`` the store-only observer uses to complete
    and release the lease. ``identity`` / ``owner`` are ``None`` for a non-idempotent request."""

    authorized: AuthorizedWrite[Any]
    identity: Identity | None
    owner: str | None

    @property
    def body(self) -> Any:
        return self.authorized.body


class Replay(Exception):
    """Raised on an idempotency HIT so the registered exception handler serves the cached response
    (byte-exact) without executing the handler."""

    def __init__(self, response_status: int | None, response_body: Any) -> None:
        self.response_status = response_status
        self.response_body = response_body


def canonical_digest(body_bytes: bytes, content_type: str) -> bytes:
    """BYTE-EXACT canonical digest (no semantic JSON equivalence): domain-separated SHA-256 over
    the content-type and the exact received body bytes. A whitespace/key-order change → different
    digest → 409, never a false replay. Always 32 bytes."""
    return hashlib.sha256(
        _DIGEST_DOMAIN + b"\x00" + content_type.encode("latin-1") + b"\x00" + body_bytes
    ).digest()


def build_identity(auth: Any, method: str, operation: str, namespace: str, key: str) -> Identity:
    """Principal- and operation-bound identity: (issuer, subject, presence, method, operation,
    authorized namespace, idempotency key)."""
    return (auth.issuer, auth.subject, auth.presence, method, operation, namespace, key)


def make_idempotency_dependency(
    authz_dependency: Callable[..., Awaitable[AuthorizedWrite[Any]]],
) -> Callable[..., Awaitable[IdempotentContext]]:
    """Build the idempotency dependency for a capture route, depending on that route's authz edge."""

    async def _dep(
        request: Request,
        authorized: AuthorizedWrite[Any] = Depends(authz_dependency),
        cache: IdempotencyLeaseCache = Depends(get_idempotency_lease_cache),
    ) -> IdempotentContext:
        keys = request.headers.getlist("idempotency-key")
        if len(keys) > 1:
            raise APIError(
                status_code=400, code="BAD_REQUEST", detail="duplicate Idempotency-Key header"
            )
        if not keys:
            return IdempotentContext(authorized=authorized, identity=None, owner=None)

        key = keys[0]
        route = request.scope["route"]
        operation = route.operation_id or route.path
        identity = build_identity(
            authorized.auth, request.method, operation, authorized.namespace, key
        )
        digest = canonical_digest(await request.body(), request.headers.get("content-type", ""))
        owner = secrets.token_hex(
            16
        )  # collision-resistant PER REQUEST; never derived from identity

        status, body, code = cache.acquire(identity, owner, digest=digest)
        if status == "hit":
            raise Replay(code, body)
        if status == "conflict":
            raise APIError(
                status_code=409,
                code="CONFLICT",
                detail="Idempotency-Key reused with a different request body",
            )
        if status == "in_flight":
            raise APIError(
                status_code=409,
                code="CONFLICT",
                detail="a request with this Idempotency-Key is already in flight; retry",
            )

        # acquired: establish the observer-visible lease state. If that fails for any reason,
        # release the lease so an acquired-but-unpublished slot can never leak.
        try:
            request.state.idem = {
                "identity": identity,
                "owner": owner,
                "digest": digest,
                "eligible": True,
            }
        except BaseException:
            cache.release(identity, owner)
            raise
        return IdempotentContext(authorized=authorized, identity=identity, owner=owner)

    return _dep


__all__ = [
    "IdempotentContext",
    "Replay",
    "build_identity",
    "canonical_digest",
    "make_idempotency_dependency",
]
