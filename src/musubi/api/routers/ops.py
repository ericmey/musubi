"""Operational endpoints — health, status, metrics passthrough."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from qdrant_client import QdrantClient

from musubi.api.dependencies import get_qdrant_client
from musubi.api.responses import ComponentStatus, HealthResponse, StatusResponse

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
    """Per-component readiness. Tries a cheap operation against each
    dependency (Qdrant collections list; TEI / Ollama health checks land
    in slice-ops-observability)."""
    components: dict[str, ComponentStatus] = {}
    try:
        qdrant.get_collections()
        components["qdrant"] = ComponentStatus(name="qdrant", healthy=True)
    except Exception as exc:
        components["qdrant"] = ComponentStatus(name="qdrant", healthy=False, detail=repr(exc))
    # TEI / Ollama probes live in slice-ops-observability; these are
    # placeholders so the response shape is stable today.
    components["tei"] = ComponentStatus(
        name="tei",
        healthy=True,
        detail="probe deferred to slice-ops-observability",
    )
    components["ollama"] = ComponentStatus(
        name="ollama",
        healthy=True,
        detail="probe deferred to slice-ops-observability",
    )
    overall = "ok" if all(c.healthy for c in components.values()) else "degraded"
    return StatusResponse(status=overall, components=components)


@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus-format metrics passthrough.

    The metrics surface ships in slice-ops-observability; this route
    returns an empty body with the correct content type so adapters can
    point Prometheus at it without errors.
    """
    return Response(
        content="# Musubi metrics — exporter wiring deferred to slice-ops-observability\n",
        media_type="text/plain; version=0.0.4",
    )


__all__ = ["router"]
