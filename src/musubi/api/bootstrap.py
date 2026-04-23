"""Production app bootstrap — wire real Qdrant + TEI + plane factories.

`musubi.api.dependencies` ships every plane factory as
``raise NotImplementedError`` per the ADR-punted-deps-fail-loud
rule. Unit tests override via ``app.dependency_overrides``; production
needs an explicit wiring step. This module is that step.

Per slice ``slice-api-app-bootstrap``:

- :func:`bootstrap_production_app` is the single entry point. It
  constructs real :class:`QdrantClient` + a TEI-backed composite
  :class:`Embedder` from :class:`Settings`, then installs
  per-plane / per-service factories on the app's dependency map.
- Each dep is health-probed before its factory installs; on
  exhaustion of the retry budget, the bootstrap raises a typed
  :class:`BootstrapError` naming the failing dep so operator
  alerts/log greps surface the right component.
- :func:`_should_bootstrap` is the gate ``create_app()`` uses to
  decide whether to call this on its way up. The gate respects
  ``settings.musubi_skip_bootstrap`` (explicit escape hatch for
  tests that don't go through the ``app_factory`` fixture) and the
  presence of any pre-installed dependency overrides (the
  ``app_factory`` fixture's path).

Idempotency: re-invoking ``bootstrap_production_app`` cleanly
re-installs every override (last-write-wins on the dict). Safe to
call from a hot reload.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI
from qdrant_client import QdrantClient

from musubi.api.dependencies import (
    get_artifact_plane,
    get_concept_plane,
    get_curated_plane,
    get_embedder,
    get_episodic_plane,
    get_lifecycle_service,
    get_qdrant_client,
    get_reranker,
    get_thoughts_plane,
)
from musubi.embedding import Embedder, TEIDenseClient, TEIRerankerClient, TEISparseClient
from musubi.planes.artifact import ArtifactPlane
from musubi.planes.concept import ConceptPlane
from musubi.planes.curated import CuratedPlane
from musubi.planes.episodic import EpisodicPlane
from musubi.planes.thoughts import ThoughtsPlane
from musubi.settings import Settings

_DEFAULT_RETRY_ATTEMPTS = 5
_DEFAULT_RETRY_BACKOFF_S = 1.0


class BootstrapError(Exception):
    """Raised when a production dependency can't be reached at boot.

    ``dep`` names the failing component (``"qdrant"``, ``"tei"``, etc.)
    so alerts + log greps surface the right dashboard panel.
    """

    def __init__(self, *, dep: str, detail: str) -> None:
        super().__init__(f"bootstrap dep {dep!r} unreachable: {detail}")
        self.dep = dep
        self.detail = detail


# ---------------------------------------------------------------------------
# TEI composite — implements the Embedder protocol by delegating each
# method to one of the three TEI clients. Per [[06-ingestion/embedding-strategy]],
# planes consume a single Embedder; the composite hides the three-service
# decomposition from the rest of the codebase.
# ---------------------------------------------------------------------------


class _TEICompositeEmbedder:
    """Embedder protocol implementation backed by three TEI services."""

    def __init__(
        self,
        *,
        dense: TEIDenseClient,
        sparse: TEISparseClient,
        reranker: TEIRerankerClient,
    ) -> None:
        self._dense = dense
        self._sparse = sparse
        self._reranker = reranker

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        return await self._dense.embed_dense(texts)

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        return await self._sparse.embed_sparse(texts)

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        return await self._reranker.rerank(query, candidates)


# ---------------------------------------------------------------------------
# Health probes — each runs once per attempt; bootstrap retries the whole
# probe sequence up to ``retry_attempts`` times before raising BootstrapError.
# ---------------------------------------------------------------------------


def _probe_qdrant(client: QdrantClient) -> None:
    """Cheap probe: list collections. Raises whatever the client raises
    on connect failure / auth failure / timeout."""
    client.get_collections()


def _probe_tei(dense_url: str) -> None:
    """Cheap synchronous probe: GET ``{dense_url}/health``. Bootstrap
    runs from sync context inside ``create_app()``; the TEI clients
    are async (uvicorn loop), so we can't call them with
    ``asyncio.run`` here without "cannot be called from a running
    event loop". Direct sync httpx hits the same endpoint TEI's
    docker healthcheck uses."""
    import httpx

    with httpx.Client(timeout=5.0) as client:
        resp = client.get(f"{dense_url.rstrip('/')}/health")
        resp.raise_for_status()


def _retry(probe: Any, *, dep: str, attempts: int, backoff_s: float) -> None:
    """Run ``probe()`` up to ``attempts`` times with linear backoff;
    raise :class:`BootstrapError` on exhaustion."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            probe()
            return
        except Exception as exc:
            last_exc = exc
            if attempt < attempts and backoff_s > 0:
                time.sleep(backoff_s)
    raise BootstrapError(dep=dep, detail=repr(last_exc))


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def bootstrap_production_app(
    app: FastAPI,
    settings: Settings,
    *,
    retry_attempts: int = _DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_s: float = _DEFAULT_RETRY_BACKOFF_S,
) -> None:
    """Install production dependency overrides on ``app``.

    Constructs real Qdrant + TEI clients from ``settings``, probes
    each, then wires every plane / service factory on the app's
    dependency map. Idempotent — safe to call multiple times.

    Raises :class:`BootstrapError` if any dep stays unreachable after
    ``retry_attempts`` probes; preserves the fail-loud invariant the
    pre-bootstrap ``NotImplementedError`` stubs encoded.
    """
    # qdrant-client defaults to https=True when api_key is supplied;
    # honour MUSUBI_ALLOW_PLAINTEXT explicitly so test-env / dev
    # plaintext Qdrant doesn't ssl-handshake-fail against an HTTP
    # endpoint. Production deploys leave the flag at its default
    # (False) so https=True is the production default.
    qdrant = QdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        api_key=settings.qdrant_api_key.get_secret_value(),
        https=not settings.musubi_allow_plaintext,
    )
    _retry(
        lambda: _probe_qdrant(qdrant),
        dep="qdrant",
        attempts=retry_attempts,
        backoff_s=retry_backoff_s,
    )

    # Ensure the canonical Musubi collections exist on this Qdrant
    # before any plane.create() touches them. Idempotent: bootstrap()
    # short-circuits per-collection if already present.
    from musubi.store import bootstrap as bootstrap_collections

    bootstrap_collections(qdrant)

    dense = TEIDenseClient(base_url=str(settings.tei_dense_url))
    sparse = TEISparseClient(base_url=str(settings.tei_sparse_url))
    reranker = TEIRerankerClient(base_url=str(settings.tei_reranker_url))
    _retry(
        lambda: _probe_tei(str(settings.tei_dense_url)),
        dep="tei",
        attempts=retry_attempts,
        backoff_s=retry_backoff_s,
    )

    embedder: Embedder = _TEICompositeEmbedder(dense=dense, sparse=sparse, reranker=reranker)

    # Every override below is a fresh closure-captured factory; calling
    # bootstrap a second time replaces the dict entries cleanly (idempotent).
    app.dependency_overrides[get_qdrant_client] = lambda: qdrant
    app.dependency_overrides[get_embedder] = lambda: embedder
    # Orchestration takes a rerank client as its own arg (the composite
    # embedder also carries one, but passing it separately keeps the
    # retrieve pipeline's signature honest + tests easier).
    app.dependency_overrides[get_reranker] = lambda: reranker
    app.dependency_overrides[get_episodic_plane] = lambda: EpisodicPlane(
        client=qdrant, embedder=embedder
    )
    app.dependency_overrides[get_curated_plane] = lambda: CuratedPlane(
        client=qdrant, embedder=embedder
    )
    app.dependency_overrides[get_concept_plane] = lambda: ConceptPlane(
        client=qdrant, embedder=embedder
    )
    app.dependency_overrides[get_artifact_plane] = lambda: ArtifactPlane(
        client=qdrant, embedder=embedder
    )
    app.dependency_overrides[get_thoughts_plane] = lambda: ThoughtsPlane(
        client=qdrant, embedder=embedder
    )
    # Lifecycle service: surface a minimal handle for the /v1/lifecycle/*
    # endpoints. The lifecycle worker itself is a separate process
    # (slice-lifecycle-*); the API just needs a queryable handle for the
    # status endpoints. Today that's a thin wrapper around Qdrant + the
    # event ledger; the bootstrap supplies the same dict every consumer
    # uses so the surface is uniform.
    app.dependency_overrides[get_lifecycle_service] = lambda: {
        "qdrant": qdrant,
        "embedder": embedder,
    }


def _should_bootstrap(app: FastAPI, settings: Settings) -> bool:
    """Decide whether ``create_app()`` should call
    :func:`bootstrap_production_app` on the way up.

    Bootstrap runs UNLESS:
    - ``settings.musubi_skip_bootstrap`` is True (explicit opt-out for
      tests that don't go through the app_factory fixture), OR
    - The app already has dependency overrides installed (the
      app_factory fixture path — test pre-installed its overrides
      before create_app returned).
    """
    if getattr(settings, "musubi_skip_bootstrap", False):
        return False
    return not app.dependency_overrides


__all__ = [
    "BootstrapError",
    "bootstrap_production_app",
]
