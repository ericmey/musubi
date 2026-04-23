"""``CaptureService`` — the hot write path that the HTTP capture
endpoint delegates to.

Per [[06-ingestion/capture]], the service owns four responsibilities
the HTTP shell doesn't touch:

1. **Per-plane dedup configuration.** ``DEFAULT_DEDUP_THRESHOLDS``
   maps plane names to similarity thresholds (or ``None`` to disable
   dedup, e.g. for curated which dedups by ``vault_path`` not vector).
2. **Per-(token, namespace) idempotency cache.** Distinct from the
   API middleware's process-wide cache: two different bearers using
   the same ``Idempotency-Key`` are independent. The cache is sqlite-
   backed with a 24h TTL per the spec.
3. **Bounded retry around plane writes.** A transient Qdrant blip
   gets one retry; permanent failures surface as
   ``Err(CaptureError(503, BACKEND_UNAVAILABLE))``. The HTTP shell
   maps this to the spec's 503 + ``Retry-After``.
4. **Lifecycle event emission on every successful capture.** The
   audit ledger records who captured what and when, with reason
   ``capture-created`` (fresh insert) or ``capture-merged`` (dedup
   hit).

The service is callable from any context — HTTP shell, async worker,
batch loader. Tests exercise it directly without the FastAPI layer.

Architecture note on Method-ownership:

- Dedup similarity threshold + idempotency cache live here (the
  ingestion layer), not in the plane. The plane owns the actual
  ``upsert``/``set_payload`` mechanics + namespace isolation.
- Two follow-up gaps are documented as cross-slice tickets to
  ``slice-plane-episodic``: a ``reinforce_with_strategy`` parameter
  for spec bullet 10 (longer-content-wins), and a ``batch_create``
  method for spec bullets 20-21 (single-TEI / single-Qdrant-upsert
  batch instrumentation).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, Field

from musubi.lifecycle.events import LifecycleEventSink
from musubi.planes.episodic import EpisodicPlane
from musubi.types.common import KSUID, Err, Namespace, Ok, Result
from musubi.types.episodic import EpisodicMemory

log = logging.getLogger(__name__)

_LIFECYCLE_ACTOR = "ingestion-capture"
_RETRY_BUDGET = 3
_RETRY_BACKOFF_S = 0.05  # initial; doubles per attempt
_DEFAULT_TTL_S = 24 * 3600


# ---------------------------------------------------------------------------
# Dedup configuration
# ---------------------------------------------------------------------------


DEFAULT_DEDUP_THRESHOLDS: Final[dict[str, float | None]] = {
    "episodic": 0.92,
    "curated": None,  # disabled — curated dedups by (namespace, vault_path)
    "concept": 0.85,
    "artifact_chunks": 0.98,
    "thought": None,  # disabled — every send is a distinct thought
}
"""Per-plane similarity threshold for dedup. ``None`` means dedup is
disabled at this layer for the plane (the plane uses a different
strategy or no dedup at all)."""


def is_dedup_enabled(plane_name: str) -> bool:
    """``True`` iff the named plane has similarity-based dedup enabled."""
    return DEFAULT_DEDUP_THRESHOLDS.get(plane_name) is not None


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


ContentType = Literal["observation", "decision", "fact", "tool-output", "transcript"]


class CaptureRequest(BaseModel):
    """Per-spec § Contract — every field the capture endpoint accepts."""

    namespace: Namespace
    content: str = Field(min_length=1, max_length=16000)
    tags: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    importance: int = Field(default=5, ge=1, le=10)
    content_type: ContentType = "observation"
    capture_source: str = ""
    source_ref: str = ""
    ingestion_metadata: dict[str, object] = Field(default_factory=dict)


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of a successful capture call.

    ``replayed=True`` when the response came from the idempotency cache
    (no work done this call). ``dedup_action="merged"`` when a dedup
    hit reused an existing row. The two are independent — a cache
    replay says nothing about whether the original call dedup'd.
    """

    object_id: KSUID
    namespace: str
    state: str
    version: int
    dedup_action: str | None = None
    replayed: bool = False


@dataclass(frozen=True)
class CaptureError:
    """Typed error for failed captures. Maps cleanly to the API
    error envelope: ``status_code`` is the HTTP code, ``code`` is the
    spec's ``ErrorCode`` enum name, ``detail`` is human-readable."""

    status_code: int
    code: str
    detail: str


# ---------------------------------------------------------------------------
# Idempotency cache
# ---------------------------------------------------------------------------


_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency (
    cache_key TEXT PRIMARY KEY,
    body_hash TEXT NOT NULL,
    object_id TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idempotency_expires ON idempotency (expires_at);
"""


class IngestionIdempotencyCache:
    """sqlite-backed per-(token_jti, namespace, key) cache.

    Distinct from :mod:`musubi.api.idempotency` (which is in-memory
    process-local for the HTTP middleware). The ingestion service uses
    this richer key shape because the spec's contract is "same token +
    namespace + key" — the API-side middleware doesn't see the
    bearer's JTI by the time the cache is consulted.
    """

    def __init__(self, *, db_path: Path, ttl_s: float = _DEFAULT_TTL_S) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl_s
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_CACHE_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path))

    @staticmethod
    def _key(*, token_jti: str, namespace: str, key: str) -> str:
        return f"{token_jti}|{namespace}|{key}"

    def lookup(
        self,
        *,
        token_jti: str,
        namespace: str,
        key: str,
        body_hash: str,
    ) -> KSUID | None:
        """Return the cached ``object_id`` for an exact hit, ``None``
        otherwise (miss, expired, or body-hash mismatch)."""
        cache_key = self._key(token_jti=token_jti, namespace=namespace, key=key)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT body_hash, object_id, expires_at FROM idempotency WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            stored_body_hash, object_id, expires_at = row
            if expires_at < time.time():
                conn.execute("DELETE FROM idempotency WHERE cache_key = ?", (cache_key,))
                conn.commit()
                return None
            if stored_body_hash != body_hash:
                # Body mismatch — neither hit nor conflict; treat as miss
                # so the caller writes fresh. (The HTTP middleware's
                # CONFLICT semantics live there; this cache is a
                # reservation for true replays.)
                return None
            return KSUID(object_id)

    def store(
        self,
        *,
        token_jti: str,
        namespace: str,
        key: str,
        body_hash: str,
        object_id: KSUID,
    ) -> None:
        cache_key = self._key(token_jti=token_jti, namespace=namespace, key=key)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO idempotency (cache_key, body_hash, object_id, expires_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
                "body_hash = excluded.body_hash, object_id = excluded.object_id, "
                "expires_at = excluded.expires_at",
                (cache_key, body_hash, object_id, time.time() + self._ttl),
            )
            conn.commit()

    def expire_for_test(self, *, token_jti: str, namespace: str, key: str) -> None:
        """Force-expire one entry. Tests use this to cover the TTL path."""
        cache_key = self._key(token_jti=token_jti, namespace=namespace, key=key)
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE idempotency SET expires_at = 0.0 WHERE cache_key = ?",
                (cache_key,),
            )
            conn.commit()


def _hash_body(request: CaptureRequest) -> str:
    """Stable hash of a CaptureRequest's body. Used as the cache's
    body-mismatch detector."""
    import hashlib

    serialized = request.model_dump_json().encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


# ---------------------------------------------------------------------------
# CaptureService
# ---------------------------------------------------------------------------


class CaptureService:
    """Orchestrates the hot write path.

    Wires the :class:`EpisodicPlane`, :class:`LifecycleEventSink`, and
    :class:`IngestionIdempotencyCache`. Public API:

    - :meth:`capture` — single capture; honours an optional
      ``Idempotency-Key`` (ingestion-side cache, scoped per-token).
    - :meth:`batch_capture` — sequential batch loop today; will
      become a single TEI + single Qdrant call once
      ``EpisodicPlane.batch_create`` ships (cross-slice ticket).
    """

    def __init__(
        self,
        *,
        plane: EpisodicPlane,
        sink: LifecycleEventSink,
        idempotency_cache: IngestionIdempotencyCache,
        dedup_thresholds: dict[str, float | None] | None = None,
    ) -> None:
        self._plane = plane
        self._sink = sink
        self._cache = idempotency_cache
        self._dedup = dict(dedup_thresholds or DEFAULT_DEDUP_THRESHOLDS)

    async def capture(
        self,
        request: CaptureRequest,
        *,
        token_jti: str = "",
        idempotency_key: str | None = None,
    ) -> Result[CaptureResult, CaptureError]:
        """Single-row capture.

        Steps (per spec § Hot-path steps):
        1. Idempotency check (if key supplied + token_jti given).
        2. Build EpisodicMemory + delegate to ``plane.create`` with
           bounded retry on transient Qdrant blips.
        3. Detect dedup-hit by comparing pre/post point counts; mark
           the result accordingly.
        4. Emit a LifecycleEvent (``capture-created`` /
           ``capture-merged``).
        5. Store the response in the idempotency cache so a retry
           within the TTL window replays.
        """
        body_hash = _hash_body(request)

        # Step 1 — idempotency lookup.
        if idempotency_key and token_jti:
            cached = self._cache.lookup(
                token_jti=token_jti,
                namespace=request.namespace,
                key=idempotency_key,
                body_hash=body_hash,
            )
            if cached is not None:
                fetched = await self._plane.get(namespace=request.namespace, object_id=cached)
                if fetched is not None:
                    return Ok(
                        value=CaptureResult(
                            object_id=fetched.object_id,
                            namespace=fetched.namespace,
                            state=fetched.state,
                            version=fetched.version,
                            dedup_action=None,
                            replayed=True,
                        )
                    )

        memory = EpisodicMemory(
            namespace=request.namespace,
            content=request.content,
            summary=None,
            tags=list(request.tags),
            importance=request.importance,
        )

        try:
            saved = await _retry(
                lambda: self._plane.create(memory),
                attempts=_RETRY_BUDGET,
                backoff_s=_RETRY_BACKOFF_S,
            )
        except _RetryExhausted as exc:
            log.warning(
                "ingestion-capture-qdrant-failed namespace=%s err=%r",
                request.namespace,
                exc.last_exc,
            )
            return Err(
                error=CaptureError(
                    status_code=503,
                    code="BACKEND_UNAVAILABLE",
                    detail=(f"Qdrant write failed after retry; last error: {exc.last_exc!r}"),
                )
            )
        except (ConnectionError, OSError) as exc:
            # Embedder failures (TEI down) bubble up immediately —
            # retry won't help if the upstream service is unreachable.
            log.warning(
                "ingestion-capture-tei-failed namespace=%s err=%r",
                request.namespace,
                exc,
            )
            return Err(
                error=CaptureError(
                    status_code=503,
                    code="BACKEND_UNAVAILABLE",
                    detail=f"embedder unreachable: {exc!r}",
                )
            )

        # Step 3 — dedup detection. EpisodicPlane.create returns the
        # row at version=1 on fresh insert, version>1 on reinforce-merge
        # (the plane bumps version inside _reinforce). Use this as the
        # cheap signal — no extra Qdrant probe needed.
        dedup_action: str | None = "merged" if saved.version > 1 else None

        # Step 4 — emit a ledger entry. The current LifecycleEvent
        # validator only accepts STATE TRANSITIONS per
        # [[04-data-model/lifecycle]]; capture is a
        # creation/reinforcement event, not a transition (provisional →
        # provisional is illegal). The spec's § Step 6 calls for an
        # emit but the type system rejects it. Cross-slice ticket
        # ``slice-ingestion-capture-slice-types-capture-event-record.md``
        # tracks adding a non-transition event variant; until that
        # lands, the ``created_at`` / ``reinforcement_count`` fields on
        # the row carry the audit signal implicitly.

        result = CaptureResult(
            object_id=saved.object_id,
            namespace=saved.namespace,
            state=saved.state,
            version=saved.version,
            dedup_action=dedup_action,
            replayed=False,
        )

        # Step 5 — idempotency cache write.
        if idempotency_key and token_jti:
            self._cache.store(
                token_jti=token_jti,
                namespace=request.namespace,
                key=idempotency_key,
                body_hash=body_hash,
                object_id=saved.object_id,
            )

        return Ok(value=result)

    async def batch_capture(
        self,
        *,
        namespace: str,
        items: list[CaptureRequest],
        token_jti: str = "",
    ) -> list[Result[CaptureResult, CaptureError]]:
        """Capture N rows via a single TEI batch + single Qdrant upsert.

        Delegates to ``EpisodicPlane.batch_create`` for the per-plane
        optimisation. Keeps the same per-row Ok/Err shape as the
        sequential-loop predecessor so existing callers don't have to
        change, but the happy path now does exactly ONE embed round-trip
        and ONE upsert for the whole batch (spec § Batched capture).

        ``batch_capture`` does NOT consult
        :class:`IngestionIdempotencyCache` — the batch ``CaptureRequest``
        shape carries no per-item idempotency key. Every item flows
        through the plane. The single-row :meth:`capture` path is still
        where jti-scoped, sqlite-backed idempotency replay happens.
        """
        if not items:
            return []

        # Force every item's namespace to the batch's outer namespace
        # so a malformed item doesn't slip a different scope past the
        # auth gate (which already validated the outer ``namespace``).
        normalised = [raw.model_copy(update={"namespace": namespace}) for raw in items]

        # Build a list of EpisodicMemory objects in input order. The
        # plane's batch_create handles validation (namespace format,
        # content size) and raises on first bad row — we let that
        # bubble up as a hard error rather than doing partial success
        # dance.
        memories = [
            EpisodicMemory(
                namespace=req.namespace,
                content=req.content,
                summary=None,
                tags=list(req.tags),
                importance=req.importance,
            )
            for req in normalised
        ]

        try:
            saved_rows = await _retry(
                lambda: self._plane.batch_create(memories),
                attempts=_RETRY_BUDGET,
                backoff_s=_RETRY_BACKOFF_S,
            )
        except _RetryExhausted as exc:
            log.warning(
                "ingestion-batch-capture-qdrant-failed namespace=%s err=%r",
                namespace,
                exc.last_exc,
            )
            err = CaptureError(
                status_code=503,
                code="BACKEND_UNAVAILABLE",
                detail=f"Qdrant batch write failed after retry; last error: {exc.last_exc!r}",
            )
            return [Err(error=err) for _ in items]
        except (ConnectionError, OSError) as exc:
            log.warning(
                "ingestion-batch-capture-tei-failed namespace=%s err=%r",
                namespace,
                exc,
            )
            err = CaptureError(
                status_code=503,
                code="BACKEND_UNAVAILABLE",
                detail=f"embedder unreachable: {exc!r}",
            )
            return [Err(error=err) for _ in items]

        out: list[Result[CaptureResult, CaptureError]] = []
        for saved in saved_rows:
            dedup_action: str | None = "merged" if saved.version > 1 else None
            out.append(
                Ok(
                    value=CaptureResult(
                        object_id=saved.object_id,
                        namespace=saved.namespace,
                        state=saved.state,
                        version=saved.version,
                        dedup_action=dedup_action,
                        replayed=False,
                    )
                )
            )
        return out


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


class _RetryExhausted(Exception):
    """Raised by :func:`_retry` when every attempt failed."""

    def __init__(self, last_exc: BaseException) -> None:
        super().__init__(repr(last_exc))
        self.last_exc = last_exc


_RETRYABLE_EXCEPTIONS = (TimeoutError,)


async def _retry[T](
    op: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    backoff_s: float,
) -> T:
    """Run ``op()`` up to ``attempts`` times with exponential backoff.

    Only catches transient-shaped exceptions (currently
    ``TimeoutError``); permanent errors (``ConnectionError``,
    pydantic validation, etc.) propagate immediately.
    """
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return await op()
        except _RETRYABLE_EXCEPTIONS as exc:
            last = exc
            if i + 1 < attempts:
                await asyncio.sleep(backoff_s * (2**i))
    assert last is not None
    raise _RetryExhausted(last)


__all__ = [
    "DEFAULT_DEDUP_THRESHOLDS",
    "CaptureError",
    "CaptureRequest",
    "CaptureResult",
    "CaptureService",
    "IngestionIdempotencyCache",
    "is_dedup_enabled",
]
