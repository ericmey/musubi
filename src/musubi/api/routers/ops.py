"""Operational endpoints — health, status, metrics passthrough, debug.

``/health`` is a liveness probe (always 200 if the process serves
requests). ``/status`` is per-component readiness — populates
:class:`StatusResponse.components` with one row per dependency
(Qdrant + each TEI service + Ollama), satisfying Aoi's v0.1
health-granularity ask. ``/metrics`` exposes the in-process
Prometheus registry in text format.

``/debug/trigger-synthesis`` is an operator-scope test-hook that
runs one synthesis tick end-to-end against the live stack; the
integration harness uses it to verify degradation behaviours
(Ollama-offline scenario per Issue #119) without embedding the
lifecycle worker in the API.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient

from musubi.api.auth import require_operator
from musubi.api.dependencies import get_embedder, get_qdrant_client
from musubi.api.responses import ComponentStatus, HealthResponse, StatusResponse
from musubi.config import get_settings
from musubi.embedding import Embedder
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

    # Per-service liveness paths. TEI exposes `/health`; Ollama doesn't —
    # it returns 404 on `/health` and 200 on `/api/tags` (empty model list
    # when no model is loaded, which still proves the daemon is up).
    for name, base_url, probe_path in (
        ("tei-dense", str(settings.tei_dense_url), "/health"),
        ("tei-sparse", str(settings.tei_sparse_url), "/health"),
        ("tei-reranker", str(settings.tei_reranker_url), "/health"),
        ("ollama", str(settings.ollama_url), "/api/tags"),
    ):
        components[name] = check_component_health(
            name=name,
            url=base_url.rstrip("/") + probe_path,
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


# ---------------------------------------------------------------------------
# Debug — /v1/ops/debug/trigger-synthesis
# ---------------------------------------------------------------------------


class TriggerSynthesisRequest(BaseModel):
    """Debug-endpoint payload for :func:`trigger_synthesis`."""

    namespace: str = Field(
        description="Agent-scoped namespace prefix (e.g. 'nyla/voice'). "
        "The synthesis loop operates on '<prefix>/episodic' + '<prefix>/concept'."
    )
    simulate_ollama_offline: bool = Field(
        default=False,
        description=(
            "Wire a stub Ollama client that always returns None "
            "(simulates an unreachable LLM). When false, the endpoint "
            "returns 501 — a real Ollama client impl is future work "
            "(there's no production OllamaClient in the codebase yet; "
            "the Protocol is satisfied by workers only)."
        ),
    )


class TriggerSynthesisResponse(BaseModel):
    """Same shape as ``musubi.lifecycle.synthesis.SynthesisReport``."""

    namespace: str
    memories_selected: int
    clusters_formed: int
    concepts_created: int
    concepts_reinforced: int
    contradictions_detected: int
    cursor_advanced_to: float | None


class _NoOpOllamaClient:
    """Simulates Ollama being offline — every call returns None.

    Matches the :class:`musubi.lifecycle.synthesis.SynthesisOllamaClient`
    Protocol. When this is wired into ``synthesis_run``, the loop logs
    'LLM unavailable during synthesis_run, skipping run' and returns a
    zero-concept report — the graceful degradation Issue #119 asserts.
    """

    async def synthesize_cluster(self, cluster: object) -> None:
        return None

    async def check_contradiction(self, pair: object) -> None:
        return None


@router.post(
    "/debug/trigger-synthesis",
    response_model=TriggerSynthesisResponse,
    operation_id="debug_trigger_synthesis.bucket=default",
    dependencies=[Depends(require_operator())],
    responses={
        403: {"description": "Caller does not hold operator scope."},
        501: {
            "description": (
                "`simulate_ollama_offline=false` but no production Ollama "
                "client impl is wired in. See Issue #119 for context."
            )
        },
    },
)
async def trigger_synthesis(
    request: TriggerSynthesisRequest,
    qdrant: QdrantClient = Depends(get_qdrant_client),
    embedder: Embedder = Depends(get_embedder),
) -> TriggerSynthesisResponse:
    """Drive one synthesis tick on demand.

    Test-hook for the integration harness (Issue #119). Restricted to
    operator-scope tokens; the synthesis loop is expensive enough that
    arbitrary callers shouldn't trigger it. Production workers run
    synthesis on their own tick loop; this endpoint exists so the
    integration suite can deterministically exercise the path without
    an embedded worker.
    """
    from fastapi import HTTPException

    from musubi.lifecycle.events import LifecycleEventSink
    from musubi.lifecycle.synthesis import (
        SynthesisCursor,
        synthesis_run,
    )

    if not request.simulate_ollama_offline:
        # Real OllamaClient impl is future work — there's no production
        # implementation of SynthesisOllamaClient in the codebase today
        # (workers construct their own). Return 501 so the caller
        # surfaces the gap cleanly.
        raise HTTPException(
            status_code=501,
            detail=(
                "trigger_synthesis without simulate_ollama_offline=true "
                "needs a production OllamaClient impl; the current "
                "codebase ships only the Protocol + worker-side "
                "construction. Issue #119's offline scenario uses the "
                "simulate flag; see the spec."
            ),
        )

    settings = get_settings()
    cursor = SynthesisCursor(db_path=settings.lifecycle_sqlite_path)
    sink = LifecycleEventSink(db_path=settings.lifecycle_sqlite_path)
    ollama = _NoOpOllamaClient()

    report = await synthesis_run(
        client=qdrant,
        sink=sink,
        ollama=ollama,
        embedder=embedder,
        cursor=cursor,
        namespace=request.namespace,
    )
    return TriggerSynthesisResponse(
        namespace=report.namespace,
        memories_selected=report.memories_selected,
        clusters_formed=report.clusters_formed,
        concepts_created=report.concepts_created,
        concepts_reinforced=report.concepts_reinforced,
        contradictions_detected=report.contradictions_detected,
        cursor_advanced_to=report.cursor_advanced_to,
    )


__all__ = ["router"]
