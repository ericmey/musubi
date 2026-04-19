"""Thin httpx-backed clients for TEI (text-embeddings-inference) endpoints.

Three clients correspond to the three GPU inference services described in
[[08-deployment/gpu-inference-topology]]:

- :class:`TEIDenseClient`    — BGE-M3 dense encoder at ``/embed``.
- :class:`TEISparseClient`   — SPLADE v3 sparse encoder at ``/embed_sparse``.
- :class:`TEIRerankerClient` — BGE reranker at ``/rerank``.

Each client is independent — they talk to separate base URLs and are composed
by the caller. None of them cache embeddings; batching is the caller's
concern (see [[06-ingestion/embedding-strategy]]).

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
from typing import Any

import httpx

from musubi.embedding.base import EmbeddingError

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_BATCH_SIZE = 64
_DEFAULT_MAX_INPUT_CHARS = 2048
_DEFAULT_RETRY_BACKOFF = 0.05


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
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    retry_backoff: float,
) -> Any:
    """POST ``payload`` as JSON; raise :class:`EmbeddingError` on failure."""
    url = f"{base_url.rstrip('/')}{path}"
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
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
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout
        self._max_batch_size = max_batch_size
        self._max_input_chars = max_input_chars
        self._retry_backoff = retry_backoff

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for batch in _chunks(texts, self._max_batch_size):
            data = await _post_json(
                self._base_url,
                "/embed",
                {"inputs": [_truncate(text, self._max_input_chars) for text in batch]},
                timeout=self._timeout,
                retry_backoff=self._retry_backoff,
            )
            # TEI returns list[list[float]] in input order.
            out.extend([[float(x) for x in vec] for vec in data])
        return out


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
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout
        self._max_batch_size = max_batch_size
        self._max_input_chars = max_input_chars
        self._retry_backoff = retry_backoff

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        if not texts:
            return []
        out: list[dict[int, float]] = []
        for batch in _chunks(texts, self._max_batch_size):
            data = await _post_json(
                self._base_url,
                "/embed_sparse",
                {"inputs": [_truncate(text, self._max_input_chars) for text in batch]},
                timeout=self._timeout,
                retry_backoff=self._retry_backoff,
            )
            # data: list[list[{"index": int, "value": float}]]
            out.extend(
                [{int(entry["index"]): float(entry["value"]) for entry in row} for row in data]
            )
        return out


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
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout
        self._max_input_chars = max_input_chars
        self._retry_backoff = retry_backoff

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        if not candidates:
            return []
        data = await _post_json(
            self._base_url,
            "/rerank",
            {
                "query": _truncate(query, self._max_input_chars),
                "texts": [_truncate(candidate, self._max_input_chars) for candidate in candidates],
            },
            timeout=self._timeout,
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


__all__ = ["TEIDenseClient", "TEIRerankerClient", "TEISparseClient"]
