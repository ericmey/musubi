"""FastAPI dependency providers for the canonical API.

Every router function consumes its planes / Qdrant client / settings
through these providers, never instantiating them directly. Test
fixtures override them via ``app.dependency_overrides`` to inject
in-memory Qdrant and fake embedders without monkey-patching imports.

Production behaviour: the default plane factories raise
``NotImplementedError`` because production wiring (the
:class:`musubi.embedding.tei.TEIEmbedder`, the lifecycle worker's
``CuratedPlane`` instance, etc.) is the responsibility of the
deploy-side bootstrap (slice-ops-compose). Tests must override; running
``create_app()`` and calling routes without overriding the plane deps
will fail loudly per the ADR-punted-deps rule.
"""

from __future__ import annotations

from functools import lru_cache

from qdrant_client import QdrantClient

from musubi.config import get_settings
from musubi.embedding import Embedder, TEIRerankerClient
from musubi.planes.artifact import ArtifactPlane
from musubi.planes.concept import ConceptPlane
from musubi.planes.curated import CuratedPlane
from musubi.planes.episodic import EpisodicPlane
from musubi.planes.thoughts import ThoughtsPlane
from musubi.settings import Settings


def get_settings_dep() -> Settings:
    """Return process-wide settings. Overridden in tests."""
    return get_settings()


@lru_cache(maxsize=1)
def _build_qdrant_client_default() -> QdrantClient:
    """Lazy production-default Qdrant client; constructed on first use."""
    settings = get_settings()
    return QdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        api_key=settings.qdrant_api_key.get_secret_value(),
    )


def get_qdrant_client() -> QdrantClient:
    """Return a Qdrant client. Overridden in tests with an in-memory one."""
    return _build_qdrant_client_default()


def get_episodic_plane() -> EpisodicPlane:
    raise NotImplementedError(
        "EpisodicPlane is not configured. Override "
        "app.dependency_overrides[get_episodic_plane] in tests, or wire "
        "production deps via the deploy-side bootstrap (slice-ops-compose). "
        "Failing closed per the ADR-punted-deps-fail-loud rule."
    )


def get_curated_plane() -> CuratedPlane:
    raise NotImplementedError(
        "CuratedPlane is not configured. Override via app.dependency_overrides in tests."
    )


def get_concept_plane() -> ConceptPlane:
    raise NotImplementedError(
        "ConceptPlane is not configured. Override via app.dependency_overrides in tests."
    )


def get_artifact_plane() -> ArtifactPlane:
    raise NotImplementedError(
        "ArtifactPlane is not configured. Override via app.dependency_overrides in tests."
    )


def get_thoughts_plane() -> ThoughtsPlane:
    raise NotImplementedError(
        "ThoughtsPlane is not configured. Override via app.dependency_overrides in tests, "
        "or wire production deps via slice-api-app-bootstrap."
    )


def get_embedder() -> Embedder:
    raise NotImplementedError(
        "Embedder is not configured. Override via app.dependency_overrides in tests, "
        "or wire production deps via slice-api-app-bootstrap."
    )


def get_reranker() -> TEIRerankerClient:
    """Cross-encoder reranker used by deep-path retrieval.

    Separate from the composite embedder on purpose: the retrieval
    orchestration signature takes ``reranker`` as its own argument,
    and tests need to be able to swap the reranker in isolation
    (e.g. to assert that deep mode actually called ``.rerank()``).
    """
    raise NotImplementedError(
        "Reranker is not configured. Override via app.dependency_overrides in tests, "
        "or wire production deps via slice-api-app-bootstrap."
    )


def get_lifecycle_service() -> object:
    """Per-process lifecycle handle for the /v1/lifecycle/* endpoints.

    The lifecycle worker runs out-of-band (slice-lifecycle-*); the API
    just needs a queryable handle for status surfacing. The bootstrap
    supplies a uniform dict-shaped handle.
    """
    raise NotImplementedError(
        "lifecycle service is not configured. Override via "
        "app.dependency_overrides in tests, or wire production deps via "
        "slice-api-app-bootstrap."
    )


__all__ = [
    "get_artifact_plane",
    "get_concept_plane",
    "get_curated_plane",
    "get_embedder",
    "get_episodic_plane",
    "get_lifecycle_service",
    "get_qdrant_client",
    "get_reranker",
    "get_settings_dep",
    "get_thoughts_plane",
]
