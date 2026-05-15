"""Thin httpx-backed clients for TEI (text-embeddings-inference) endpoints.

Three clients correspond to the three GPU inference services described in
[[08-deployment/gpu-inference-topology]]:

- :class:`TEIDenseClient`    — BGE-M3 dense encoder at ``/embed``.
- :class:`TEISparseClient`   — SPLADE v3 sparse encoder at ``/embed_sparse``.
- :class:`TEIRerankerClient` — BGE reranker at ``/rerank``.

Each client is independent — they talk to separate base URLs and are composed
by the caller. None of them cache embeddings; batching is the caller's
concern (see [[06-ingestion/embedding-strategy]]).

Connection reuse contract:

- Each client owns a single long-lived ``httpx.AsyncClient`` with HTTP/1.1
  keepalive. Bootstrap constructs the TEI clients once per process, so this
  gives us a warm pool for the lifetime of the worker. Without it, every
  request pays the full TCP connect + HTTP handshake cost — which shows up
  as a ~10x slowdown under concurrency because the pool never warms up.
- :meth:`aclose` must be called at process shutdown for clean teardown.
  Bootstrap wires this to the FastAPI ``shutdown`` event.

Error handling contract:

- 5xx responses raise :class:`EmbeddingError` with ``status_code`` set.
- Network-level failures (``httpx.ConnectError``, ``httpx.TimeoutException``)
  raise :class:`EmbeddingError` with ``status_code=None``.
- 4xx responses raise :class:`EmbeddingError` with the response status, so
  callers don't need to know about httpx.
"""

from __future__ import annotations

import asyncio
import random
import threading
import weakref
from typing import Any

import httpx

from musubi.embedding.base import EmbeddingError

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_BATCH_SIZE = 64
_DEFAULT_MAX_INPUT_CHARS = 2048
_DEFAULT_RETRY_BACKOFF = 0.05

# Per-client connection pool. 100 max inflight is generous — Musubi's worst
# realistic concurrency (voice call + 2 browser agents, each with a few
# parallel plane queries) tops out around ~15. 20 keepalives is what httpx
# recommends for a long-lived service-to-service client; it's the size of
# the idle pool we hold open between bursts.
_DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)


def _raise_for_httpx(exc: httpx.HTTPError) -> None:
    """Translate an ``httpx`` network-level error into an ``EmbeddingError``."""
    raise EmbeddingError(
        f"TEI request failed: {type(exc).__name__}: {exc}",
        status_code=None,
    ) from exc


def _raise_for_status(response: httpx.Response) -> None:
    """Translate a non-2xx ``httpx.Response`` into an ``EmbeddingError``."""
    if response.is_success:
        return
    # Prefer the response body for the error message; fall back to reason.
    try:
        body = response.text
    except Exception:  # pragma: no cover - defensive
        body = ""
    message = body.strip() or f"HTTP {response.status_code}"
    raise EmbeddingError(
        f"TEI returned {response.status_code}: {message}",
        status_code=response.status_code,
    )


async def _post_json(
    client: httpx.AsyncClient,
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    retry_backoff: float,
) -> Any:
    """POST ``payload`` as JSON through ``client``; raise :class:`EmbeddingError`
    on failure.

    The caller owns ``client`` — we reuse it across calls so keepalive +
    connection pooling actually kick in. Creating a fresh ``AsyncClient``
    per call (the previous pattern) costs ~50-100ms in TCP handshake under
    contention; reusing the pooled client is where the speedup lives.
    """
    url = f"{base_url.rstrip('/')}{path}"
    for attempt in range(2):
        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            _raise_for_httpx(exc)
            raise  # pragma: no cover — _raise_for_httpx always raises
        if _should_retry(response, attempt):
            await _sleep_with_jitter(retry_backoff)
            continue
        _raise_for_status(response)
        return response.json()
    raise AssertionError("retry loop must return or raise")  # pragma: no cover


def _should_retry(response: httpx.Response, attempt: int) -> bool:
    return attempt == 0 and 500 <= response.status_code <= 599


async def _sleep_with_jitter(backoff: float) -> None:
    if backoff <= 0.0:
        return
    await asyncio.sleep(random.uniform(0.0, backoff))


def _chunks(texts: list[str], max_batch_size: int) -> list[list[str]]:
    if max_batch_size <= 0:
        raise ValueError("max_batch_size must be positive")
    return [texts[i : i + max_batch_size] for i in range(0, len(texts), max_batch_size)]


def _truncate(text: str, max_input_chars: int) -> str:
    if max_input_chars <= 0:
        raise ValueError("max_input_chars must be positive")
    return text[:max_input_chars]


def _build_client(timeout: float, limits: httpx.Limits) -> httpx.AsyncClient:
    """Factory for the per-instance pooled HTTP client.

    Constructing ``AsyncClient`` does no I/O — the pool is lazy until the
    first request. So building it in ``__init__`` is safe even when the
    event loop hasn't started yet (e.g. during FastAPI app bootstrap).
    """
    return httpx.AsyncClient(timeout=timeout, limits=limits)


class _LoopBoundAsyncClient:
    """Per-event-loop ``httpx.AsyncClient`` cache.

    The lifecycle worker spawns a fresh asyncio loop per tick (job funcs
    call ``asyncio.run()`` inside ``asyncio.to_thread``), and ticks can
    run concurrently in different worker threads. A long-lived
    ``httpx.AsyncClient`` constructed at process start can't safely be
    reused across loops — its connection pool holds connections whose
    transports bind to the loop that opened them. Once that loop closes,
    the pool's next maintenance pass (during a NEW request on a NEW
    loop) tries to ``aclose`` those stale connections and trips
    ``RuntimeError: Event loop is closed``.

    Symptom: lifecycle-worker's ``reflection_digest`` crashed daily at
    06:00 UTC with the closed-loop traceback through
    ``httpcore.connection_pool._close_connections``.

    Cache shape: a ``WeakKeyDictionary[loop -> AsyncClient]`` protected
    by a ``threading.Lock``. Per-loop entries auto-evict when the loop
    is garbage-collected at ``asyncio.run`` exit — no manual cleanup
    needed for dead loops. The lock guards the dict mutations so two
    worker threads handling concurrent ticks on different loops can't
    race on insertion.

    Within one loop the same ``AsyncClient`` is reused for the whole
    tick so HTTP/1.1 keepalive + connection pooling are still in play.
    """

    __slots__ = ("_clients", "_closed", "_limits", "_lock", "_timeout")

    def __init__(self, *, timeout: float, limits: httpx.Limits) -> None:
        self._timeout = timeout
        self._limits = limits
        self._lock = threading.Lock()
        self._clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
            weakref.WeakKeyDictionary()
        )
        self._closed: bool = False

    @property
    def is_closed(self) -> bool:
        """``True`` iff :meth:`aclose` has been called.

        Distinct from the per-loop ``httpx.AsyncClient``'s own
        ``is_closed``: this wrapper outlives any one loop's client,
        so the wrapper's closed-state tracks the *intentional* shutdown
        signal, not the lifecycle of a single underlying client.
        """
        return self._closed

    def get(self) -> httpx.AsyncClient:
        """Return an ``AsyncClient`` bound to the currently-running loop.

        Concurrent ticks on different loops each get their own entry;
        re-entries on the same loop reuse the cached client. A loop
        that's been closed (e.g. by an early ``loop.close()`` outside
        ``asyncio.run``) gets a fresh client too.
        """
        if self._closed:
            raise RuntimeError(
                "TEI client used after aclose(); construct a new client to continue."
            )
        loop = asyncio.get_running_loop()
        with self._lock:
            cached = self._clients.get(loop)
            if cached is None or loop.is_closed():
                cached = _build_client(self._timeout, self._limits)
                self._clients[loop] = cached
            return cached

    async def aclose(self) -> None:
        """Close the current loop's cached client, if any. Idempotent.

        Only the entry for the currently-running loop is awaited;
        clients bound to other (possibly dead) loops are dropped
        without attempting cleanup — that path is exactly what this
        wrapper exists to avoid.
        """
        self._closed = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self._lock:
            current = self._clients.pop(loop, None) if loop is not None else None
            self._clients.clear()
        if current is not None and loop is not None and not loop.is_closed():
            await current.aclose()


class TEIDenseClient:
    """Client for a TEI dense encoder endpoint.

    TEI dense endpoints accept ``{"inputs": [...]}`` and return a JSON array
    of vectors, one per input, in input order.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = _DEFAULT_TIMEOUT,
        max_batch_size: int = _DEFAULT_MAX_BATCH_SIZE,
        max_input_chars: int = _DEFAULT_MAX_INPUT_CHARS,
        retry_backoff: float = _DEFAULT_RETRY_BACKOFF,
        limits: httpx.Limits = _DEFAULT_LIMITS,
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout
        self._max_batch_size = max_batch_size
        self._max_input_chars = max_input_chars
        self._retry_backoff = retry_backoff
        self._client = _LoopBoundAsyncClient(timeout=timeout, limits=limits)

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for batch in _chunks(texts, self._max_batch_size):
            data = await _post_json(
                self._client.get(),
                self._base_url,
                "/embed",
                {"inputs": [_truncate(text, self._max_input_chars) for text in batch]},
                retry_backoff=self._retry_backoff,
            )
            # TEI returns list[list[float]] in input order.
            out.extend([[float(x) for x in vec] for vec in data])
        return out

    async def aclose(self) -> None:
        """Close the underlying httpx connection pool. Idempotent."""
        await self._client.aclose()


class TEISparseClient:
    """Client for a TEI sparse encoder endpoint.

    TEI sparse endpoints accept ``{"inputs": [...]}`` and return, per input,
    a list of ``{"index": int, "value": float}`` pairs. The client flattens
    each to a ``dict[int, float]`` so downstream code sees a uniform shape.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = _DEFAULT_TIMEOUT,
        max_batch_size: int = _DEFAULT_MAX_BATCH_SIZE,
        max_input_chars: int = _DEFAULT_MAX_INPUT_CHARS,
        retry_backoff: float = _DEFAULT_RETRY_BACKOFF,
        limits: httpx.Limits = _DEFAULT_LIMITS,
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout
        self._max_batch_size = max_batch_size
        self._max_input_chars = max_input_chars
        self._retry_backoff = retry_backoff
        self._client = _LoopBoundAsyncClient(timeout=timeout, limits=limits)

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        if not texts:
            return []
        out: list[dict[int, float]] = []
        for batch in _chunks(texts, self._max_batch_size):
            data = await _post_json(
                self._client.get(),
                self._base_url,
                "/embed_sparse",
                {"inputs": [_truncate(text, self._max_input_chars) for text in batch]},
                retry_backoff=self._retry_backoff,
            )
            # data: list[list[{"index": int, "value": float}]]
            out.extend(
                [{int(entry["index"]): float(entry["value"]) for entry in row} for row in data]
            )
        return out

    async def aclose(self) -> None:
        """Close the underlying httpx connection pool. Idempotent."""
        await self._client.aclose()


class TEIRerankerClient:
    """Client for a TEI reranker endpoint.

    TEI reranker accepts ``{"query": str, "texts": [...]}`` and returns a
    list of ``{"index": int, "score": float}`` objects. The server is free
    to return them in score order, so the client reorders by ``index`` to
    match the caller's candidate order — that's the contract every consumer
    expects from an :class:`Embedder`.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = _DEFAULT_TIMEOUT,
        max_input_chars: int = _DEFAULT_MAX_INPUT_CHARS,
        retry_backoff: float = _DEFAULT_RETRY_BACKOFF,
        limits: httpx.Limits = _DEFAULT_LIMITS,
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout
        self._max_input_chars = max_input_chars
        self._retry_backoff = retry_backoff
        self._client = _LoopBoundAsyncClient(timeout=timeout, limits=limits)

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        if not candidates:
            return []
        data = await _post_json(
            self._client.get(),
            self._base_url,
            "/rerank",
            {
                "query": _truncate(query, self._max_input_chars),
                "texts": [_truncate(candidate, self._max_input_chars) for candidate in candidates],
            },
            retry_backoff=self._retry_backoff,
        )
        # data: list[{"index": int, "score": float}] — possibly out of order.
        scores = [0.0] * len(candidates)
        seen = [False] * len(candidates)
        for entry in data:
            idx = int(entry["index"])
            if 0 <= idx < len(candidates):
                scores[idx] = float(entry["score"])
                seen[idx] = True
        if not all(seen):
            missing = [i for i, got in enumerate(seen) if not got]
            raise EmbeddingError(
                f"TEI reranker response missing scores for indexes {missing}",
                status_code=None,
            )
        return scores

    async def aclose(self) -> None:
        """Close the underlying httpx connection pool. Idempotent."""
        await self._client.aclose()


__all__ = ["TEIDenseClient", "TEIRerankerClient", "TEISparseClient"]
