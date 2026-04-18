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

from typing import Any

import httpx

from musubi.embedding.base import EmbeddingError

_DEFAULT_TIMEOUT = 30.0


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
) -> Any:
    """POST ``payload`` as JSON; raise :class:`EmbeddingError` on failure."""
    url = f"{base_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        _raise_for_httpx(exc)
        raise  # pragma: no cover — _raise_for_httpx always raises
    _raise_for_status(response)
    return response.json()


class TEIDenseClient:
    """Client for a TEI dense encoder endpoint.

    TEI dense endpoints accept ``{"inputs": [...]}`` and return a JSON array
    of vectors, one per input, in input order.
    """

    def __init__(self, *, base_url: str, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url
        self._timeout = timeout

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = await _post_json(
            self._base_url,
            "/embed",
            {"inputs": list(texts)},
            timeout=self._timeout,
        )
        # TEI returns list[list[float]] in input order.
        return [[float(x) for x in vec] for vec in data]


class TEISparseClient:
    """Client for a TEI sparse encoder endpoint.

    TEI sparse endpoints accept ``{"inputs": [...]}`` and return, per input,
    a list of ``{"index": int, "value": float}`` pairs. The client flattens
    each to a ``dict[int, float]`` so downstream code sees a uniform shape.
    """

    def __init__(self, *, base_url: str, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url
        self._timeout = timeout

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        if not texts:
            return []
        data = await _post_json(
            self._base_url,
            "/embed_sparse",
            {"inputs": list(texts)},
            timeout=self._timeout,
        )
        # data: list[list[{"index": int, "value": float}]]
        return [{int(entry["index"]): float(entry["value"]) for entry in row} for row in data]


class TEIRerankerClient:
    """Client for a TEI reranker endpoint.

    TEI reranker accepts ``{"query": str, "texts": [...]}`` and returns a
    list of ``{"index": int, "score": float}`` objects. The server is free
    to return them in score order, so the client reorders by ``index`` to
    match the caller's candidate order — that's the contract every consumer
    expects from an :class:`Embedder`.
    """

    def __init__(self, *, base_url: str, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url
        self._timeout = timeout

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        if not candidates:
            return []
        data = await _post_json(
            self._base_url,
            "/rerank",
            {"query": query, "texts": list(candidates)},
            timeout=self._timeout,
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
