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
    reason="Telemetry cardinality: musubi_retrieval_warnings_total{warning,plane} must count ONCE per distinct (warning,plane) per request; the metric does not exist yet",
)
def test_telemetry_per_request_cardinality(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    from musubi.observability import registry as _reg

    async def mock_run_orchestration(*args: Any, **kwargs: Any) -> Any:
        class Envelope:
            # a degraded success where the SAME (code, plane) legitimately arises twice — the
            # per-request metric must still count it exactly once.
            def __init__(self) -> None:
                self.results: list[Any] = []
                self.warnings = ["plane_timeout_episodic", "plane_timeout_episodic"]

            def __iter__(self) -> Any:
                return iter(self.results)

        return Ok(value=Envelope())

    monkeypatch.setattr(
        "musubi.api.routers.retrieve.run_orchestration_retrieve", mock_run_orchestration
    )
    reg = _reg.default_registry()
    metric = {getattr(m, "name", None): m for m in getattr(reg, "_metrics", {}).values()}.get(
        "musubi_retrieval_warnings_total"
    )
    if metric is None:
        raise DefectStillPresent(
            "musubi_retrieval_warnings_total does not exist — per-request cardinality cannot be counted"
        )
    # (post-impl) the request would increment {warning=plane_timeout_episodic, plane=episodic} by
    # exactly 1 despite the duplicate; asserted once the metric exists.
    _post(_app(monkeypatch, api_settings))
