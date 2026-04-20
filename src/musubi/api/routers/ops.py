"""Operational endpoints — health, status, metrics passthrough.

``/health`` is a liveness probe (always 200 if the process serves
requests). ``/status`` is per-component readiness — populates
:class:`StatusResponse.components` with one row per dependency
(Qdrant + each TEI service + Ollama), satisfying Aoi's v0.1
health-granularity ask. ``/metrics`` exposes the in-process
Prometheus registry in text format.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from qdrant_client import QdrantClient

from musubi.api.dependencies import get_qdrant_client
from musubi.api.responses import ComponentStatus, HealthResponse, StatusResponse
from musubi.config import get_settings
from musubi.observability import (
    check_component_health,
    default_registry,
    render_text_format,
)

router = APIRouter(prefix="/v1/ops", tags=["ops"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe. Always 200 if the process can serve requests.
    Readiness — does the process have working dependencies — is on
    ``/v1/ops/status``."""
    return HealthResponse(status="ok")


@router.get("/status", response_model=StatusResponse)
async def status(
    qdrant: QdrantClient = Depends(get_qdrant_client),
) -> StatusResponse:
    """Per-component readiness — Qdrant + every TEI service + Ollama."""
    components: dict[str, ComponentStatus] = {}

    # Qdrant — list collections (cheap, no scan).
    try:
        qdrant.get_collections()
        components["qdrant"] = ComponentStatus(name="qdrant", healthy=True)
    except Exception as exc:
        components["qdrant"] = ComponentStatus(name="qdrant", healthy=False, detail=repr(exc))

    # TEI dense / sparse / reranker + Ollama — HTTP probe to /health.
    try:
        settings = get_settings()
    except Exception as exc:
        # Settings may fail to load in tests/CI; surface as degraded
        # without crashing the endpoint.
        for name in ("tei-dense", "tei-sparse", "tei-reranker", "ollama"):
            components[name] = ComponentStatus(
                name=name,
                healthy=False,
                detail=f"settings unavailable: {exc!r}",
            )
        overall = "degraded"
        return StatusResponse(status=overall, components=components)

    for name, base_url in (
        ("tei-dense", str(settings.tei_dense_url)),
        ("tei-sparse", str(settings.tei_sparse_url)),
        ("tei-reranker", str(settings.tei_reranker_url)),
        ("ollama", str(settings.ollama_url)),
    ):
        components[name] = check_component_health(
            name=name,
            url=base_url.rstrip("/") + "/health",
        )

    overall = "ok" if all(c.healthy for c in components.values()) else "degraded"
    return StatusResponse(status=overall, components=components)


@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus-format metrics from the in-process registry."""
    return Response(
        content=render_text_format(default_registry()),
        media_type="text/plain; version=0.0.4",
    )


__all__ = ["router"]
