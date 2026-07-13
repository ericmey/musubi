"""RET-007 — HTTP wire-shape + telemetry red contract.

Owner slice: slice-ret007-degradation (Musubi router + schema + metrics). Tests/docs only, no src.

- The router (`api/routers/retrieve.py`) strips the internal `warnings` array at the HTTP boundary,
  so a degraded 200 reaches the client as a plain success (contract §2/§3 — additive `warnings`).
- The two required Prometheus metrics (`musubi_retrieval_warnings_total`,
  `musubi_retrieval_errors_total`) do not exist yet (contract §6), and labels must be bounded to the
  allowlisted codes / fixed planes, never raw exception text.

    uv run pytest tests/api/test_ret007_http_warnings.py -v
"""

from typing import Any

import pytest

from musubi.retrieve.warnings import RetrievalWarning
from musubi.settings import Settings
from musubi.types.common import Ok


class DefectStillPresent(Exception):
    """Raised when the current code still exhibits the contract-forbidden defect."""


def test_http_wire_shape_drops_warnings(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    from fastapi.testclient import TestClient

    from musubi.api.app import create_app
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

    async def mock_run_orchestration(*args: Any, **kwargs: Any) -> Any:
        class MockOrchResult:
            def __init__(self) -> None:
                self.results: list[Any] = []
                self.warnings = [RetrievalWarning("sparse_embedding_failed", "episodic")]

            def __iter__(self) -> Any:
                return iter(self.results)

        return Ok(value=MockOrchResult())

    monkeypatch.setattr(
        "musubi.api.routers.retrieve.run_orchestration_retrieve", mock_run_orchestration
    )

    app = create_app(settings=api_settings)
    from musubi.api.dependencies import (
        get_embedder,
        get_qdrant_client,
        get_reranker,
        get_settings_dep,
    )

    app.dependency_overrides[get_settings_dep] = lambda: api_settings
    app.dependency_overrides[get_qdrant_client] = lambda: None
    app.dependency_overrides[get_embedder] = lambda: None
    app.dependency_overrides[get_reranker] = lambda: None

    client = TestClient(app)
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={
            "namespace": "test/ns/episodic",
            "query_text": "test",
            "mode": "fast",
            "planes": ["episodic"],
        },
    )
    assert response.status_code == 200, (
        f"expected 200, got {response.status_code}: {response.text[:200]}"
    )
    data = response.json()
    if "warnings" not in data:
        raise DefectStillPresent(
            "Wire-shape: HTTP response omitted the `warnings` field — degradation signals cannot reach clients"
        )
    assert data["warnings"] == ["sparse_embedding_failed"]


def test_telemetry_bounded_labels() -> None:
    """Both required metrics must exist, and `musubi_retrieval_warnings_total` must carry EXACTLY the
    bounded label set `{warning, plane}` (contract §6) — never a raw-exception-text or free-text
    label NAME. Asserted genuinely against the counter's declared `labelnames`."""
    from musubi.observability import registry as _reg

    reg = _reg.default_registry()
    metrics = {getattr(m, "name", None): m for m in getattr(reg, "_metrics", {}).values()}
    w = metrics.get("musubi_retrieval_warnings_total")
    e = metrics.get("musubi_retrieval_errors_total")
    if w is None or e is None:
        raise DefectStillPresent(
            "Telemetry: the required degradation metrics (musubi_retrieval_warnings_total, "
            "musubi_retrieval_errors_total) do not exist"
        )
    # Genuinely assert boundedness: the warnings counter's label NAMES must be exactly {warning,
    # plane} — an implementation that added an unbounded label (e.g. a raw exception string) fails.
    warn_labels = set(getattr(w, "labelnames", ()))
    if warn_labels != {"warning", "plane"}:
        raise DefectStillPresent(
            f"Telemetry: musubi_retrieval_warnings_total labels not bounded to {{warning, plane}}; got {warn_labels}"
        )
