"""RET-007 IMPLEMENTATION — total-failure status mapping (controls) + per-request telemetry cardinality (red).

Owner slice: slice-ret007-degradation-impl (#422). Tests-first, no src.

Status mapping (CONTROLS — the router's kind→status table already exists and MUST be preserved through
the impl): a total failure surfaced as `Err(RetrievalError(kind))` maps to the exact HTTP status —
timeout→503, internal→500, bad_query→400 (caller-caused, NOT relabelled 500), forbidden→403. These
pass today; they guard the contract's status semantics while the core is changed to actually RETURN
`Err` on total failure (the C5/H11 reds cover that half).

Telemetry (RED): musubi_retrieval_warnings_total{warning,plane} must count ONCE per distinct
(warning, plane) per request, and musubi_retrieval_errors_total{kind} once per failed request —
neither metric exists yet, so the cardinality cannot be honoured.

    uv run pytest tests/api/test_ret007_status_and_telemetry.py -v
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from musubi.settings import Settings
from musubi.types.common import Err, Ok


class DefectStillPresent(Exception):
    """Raised when the current code still exhibits the contract-forbidden defect."""


def _app(monkeypatch: pytest.MonkeyPatch, api_settings: Settings) -> TestClient:
    from musubi.api.app import create_app
    from musubi.api.dependencies import (
        get_embedder,
        get_qdrant_client,
        get_reranker,
        get_settings_dep,
    )
    from musubi.auth.tokens import AuthContext

    def mock_auth(*args: Any, **kwargs: Any) -> Any:
        return Ok(
            value=AuthContext(
                subject="test",
                scopes=("**:rw",),
                presence="test",
                issuer="test",
                audience="test",
                token_id="t",
            )
        )

    monkeypatch.setattr("musubi.api.routers.retrieve.authenticate_request", mock_auth)
    app = create_app(settings=api_settings)
    app.dependency_overrides[get_settings_dep] = lambda: api_settings
    app.dependency_overrides[get_qdrant_client] = lambda: None
    app.dependency_overrides[get_embedder] = lambda: None
    app.dependency_overrides[get_reranker] = lambda: None
    return TestClient(app)


def _post(client: TestClient) -> Any:
    return client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={
            "namespace": "test/ns/episodic",
            "query_text": "q",
            "mode": "fast",
            "planes": ["episodic"],
        },
    )


@pytest.mark.parametrize(
    "kind,expected",
    [("timeout", 503), ("internal", 500), ("bad_query", 400), ("forbidden", 403)],
)
def test_total_failure_status_mapping_control(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings, kind: str, expected: int
) -> None:
    """CONTROL (must stay green): a total-failure Err(kind) maps to the exact status. Guards that the
    impl never relabels a caller-caused bad_query (400) as a server 500."""
    from musubi.retrieve.orchestration import RetrievalError

    async def mock_run_orchestration(*args: Any, **kwargs: Any) -> Any:
        return Err(error=RetrievalError(kind=kind, detail="simulated total failure"))  # type: ignore[arg-type]

    monkeypatch.setattr(
        "musubi.api.routers.retrieve.run_orchestration_retrieve", mock_run_orchestration
    )
    resp = _post(_app(monkeypatch, api_settings))
    assert resp.status_code == expected, (
        f"kind={kind} expected {expected}, got {resp.status_code}: {resp.text[:200]}"
    )


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="Telemetry cardinality: musubi_retrieval_warnings_total{warning,plane} must count each distinct (warning,plane) exactly once per request (deduped) and musubi_retrieval_errors_total{kind} once per failed request; neither metric exists yet",
)
def test_telemetry_per_request_cardinality(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """Snapshot each counter's ``collect()`` before/after a single request and assert the EXACT
    delta — a degraded success carrying the SAME (code, plane) twice increments
    ``warnings_total{warning,plane}`` by exactly +1 (deduped, zero unrelated labels moved), and a
    total-failure request increments ``errors_total{kind}`` by exactly +1. Pre-impl the metrics do
    not exist, so the snapshot is ``None`` and the red fires."""
    from musubi.observability import registry as _reg

    reg = _reg.default_registry()

    def _find(name: str) -> Any:
        return next((m for m in reg._instruments() if getattr(m, "name", None) == name), None)

    def _snapshot(name: str) -> dict[tuple[str, ...], float] | None:
        metric = _find(name)
        return None if metric is None else dict(metric.collect())

    def _labelnames(name: str) -> tuple[str, ...]:
        return tuple(getattr(_find(name), "labelnames", ()))

    warn_before = _snapshot("musubi_retrieval_warnings_total")
    err_before = _snapshot("musubi_retrieval_errors_total")
    if warn_before is None or err_before is None:
        raise DefectStillPresent(
            "musubi_retrieval_warnings_total / musubi_retrieval_errors_total do not exist — "
            "per-request cardinality cannot be counted"
        )

    # --- degraded success: the SAME (code, plane) arrives twice; the request must count it ONCE ---
    async def mock_degraded(*args: Any, **kwargs: Any) -> Any:
        class Envelope:
            def __init__(self) -> None:
                self.results: list[Any] = [
                    {"plane": "episodic", "object_id": "1", "namespace": "test/ns", "score": 1.0}
                ]
                self.warnings = ["plane_timeout_episodic", "plane_timeout_episodic"]

            def __iter__(self) -> Any:
                return iter(self.results)

        return Ok(value=Envelope())

    monkeypatch.setattr("musubi.api.routers.retrieve.run_orchestration_retrieve", mock_degraded)
    _post(_app(monkeypatch, api_settings))

    warn_after = _snapshot("musubi_retrieval_warnings_total") or {}
    wkey = tuple(
        {"warning": "plane_timeout_episodic", "plane": "episodic"}[n]
        for n in _labelnames("musubi_retrieval_warnings_total")
    )
    moved = {
        k: warn_after.get(k, 0.0) - warn_before.get(k, 0.0)
        for k in set(warn_after) | set(warn_before)
        if warn_after.get(k, 0.0) - warn_before.get(k, 0.0) != 0.0
    }
    assert moved == {wkey: 1.0}, (
        f"expected exactly +1 for {wkey} and zero unrelated labels; got moved={moved}"
    )

    # --- total failure: errors_total{kind} increments exactly once for the failed request ---
    from musubi.retrieve.orchestration import RetrievalError

    async def mock_fail(*args: Any, **kwargs: Any) -> Any:
        return Err(error=RetrievalError(kind="timeout", detail="all planes timed out"))

    monkeypatch.setattr("musubi.api.routers.retrieve.run_orchestration_retrieve", mock_fail)
    _post(_app(monkeypatch, api_settings))

    err_after = _snapshot("musubi_retrieval_errors_total") or {}
    ekey = tuple({"kind": "timeout"}[n] for n in _labelnames("musubi_retrieval_errors_total"))
    edelta = err_after.get(ekey, 0.0) - err_before.get(ekey, 0.0)
    assert edelta == 1.0, f"expected exactly +1 for errors_total{ekey}, got {edelta}"
