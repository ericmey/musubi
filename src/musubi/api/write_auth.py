"""Shared ``AuthorizedWrite`` context for the body-derived capture routes (Phase B).

The idempotent JSON capture routes authorize a **body-derived** namespace. To make authorization
an explicit dependency EDGE (so the idempotency dependency can ``Depends`` on it and never run
pre-auth), each capture route declares a body-auth dependency that:

1. parses the request body ONCE (``body: <Model> = Body(...)`` in the dependency),
2. authorizes the body namespace via :func:`musubi.api.auth.authorize_namespace` (which also
   attaches the :class:`AuthContext` to ``request.state.auth``),
3. returns this :class:`AuthorizedWrite` carrying the validated auth, the authorized namespace,
   and the single parsed body.

The handler then ``Depends`` on that dependency (directly, or transitively through the
idempotency dependency) and consumes ``authorized.body`` — it does NOT re-declare the body, so
the body is parsed exactly once. The per-route dependencies live in the router modules (they own
the concrete body models); this module holds only the shared type to avoid an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from musubi.auth.tokens import AuthContext

BodyT = TypeVar("BodyT")


@dataclass(frozen=True)
class AuthorizedWrite(Generic[BodyT]):
    """The result of a body-derived authorization dependency edge.

    - ``auth``: the validated :class:`AuthContext` (issuer + subject + presence + scopes).
    - ``namespace``: the authorized body namespace.
    - ``body``: the single parsed request body instance the handler consumes.
    """

    auth: AuthContext
    namespace: str
    body: BodyT
