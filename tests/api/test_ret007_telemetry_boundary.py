"""RET-007 Blocker 3+5 — telemetry at THE shared final boundary + fail-closed boundedness.

Owner slice: slice-ret007-degradation-impl (#422).

Blocker 5: the locked design counts at the final orchestration boundary, so EVERY surface
(``/v1/retrieve``, ``/v1/context``, direct orchestration) is counted exactly once — not only the
retrieve router. These tests mock ``_run_single`` so REAL ``orchestration.retrieve`` runs through the
counting/finalize boundary (mocking ``retrieve`` itself would bypass the very thing under test).

Blocker 3: boundedness must fail closed in PRODUCTION — a non-allowlisted code/plane (or a code/plane
mismatch) must be dropped before it can become an unbounded Prometheus label or reach the wire.

    uv run pytest tests/api/test_ret007_telemetry_boundary.py -v
"""

from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from musubi.embedding.fake import FakeEmbedder
from musubi.observability.registry import default_registry
from musubi.retrieve import orchestration as orch
from musubi.retrieve.orchestration import RetrievalEnvelope, RetrievalError, RetrievalQuery
from musubi.retrieve.warnings import RetrievalWarning, plane_timeout
from musubi.settings import Settings
from musubi.types.common import Err, Ok


class _MockQdrant:
    def query_points(self, *args: Any, **kwargs: Any) -> Any:
        return type("R", (), {"points": []})()


def _snapshot(name: str) -> dict[tuple[str, ...], float]:
    reg = default_registry()
    metric = next((m for m in reg._instruments() if getattr(m, "name", None) == name), None)
    if metric is None:
        return {}
    return {k: cast(float, v) for k, v in metric.collect()}


def _labelnames(name: str) -> tuple[str, ...]:
    reg = default_registry()
    metric = next((m for m in reg._instruments() if getattr(m, "name", None) == name), None)
    return tuple(getattr(metric, "labelnames", ()))


def _moved(
    before: dict[tuple[str, ...], float], after: dict[tuple[str, ...], float]
) -> dict[tuple[str, ...], float]:
    return {
        k: after.get(k, 0.0) - before.get(k, 0.0)
        for k in set(after) | set(before)
        if after.get(k, 0.0) - before.get(k, 0.0) != 0.0
    }


async def _run_orch(monkeypatch: pytest.MonkeyPatch, single_result: Any, mode: str = "deep") -> Any:
    async def fake_single(*args: Any, **kwargs: Any) -> Any:
        return single_result

    monkeypatch.setattr("musubi.retrieve.orchestration._run_single", fake_single)
    return await orch.retrieve(
        client=cast(Any, _MockQdrant()),
        embedder=FakeEmbedder(),
        reranker=cast(Any, object()),
        query=RetrievalQuery(
            namespace="test/ns", query_text="q", mode=cast(Any, mode), planes=["episodic"]
        ),
    )


# --------------------------------------------------------------------------- #
# Blocker 5 — counted once, at the shared boundary
# --------------------------------------------------------------------------- #


async def test_orchestration_counts_warnings_once_deduped(monkeypatch: pytest.MonkeyPatch) -> None:
    before = _snapshot("musubi_retrieval_warnings_total")
    env = RetrievalEnvelope(
        results=[], warnings=(plane_timeout("episodic"), plane_timeout("episodic"))
    )
    result = await _run_orch(monkeypatch, Ok(value=env))
    assert isinstance(result, Ok)
    key = tuple(
        {"warning": "plane_timeout_episodic", "plane": "episodic"}[n]
        for n in _labelnames("musubi_retrieval_warnings_total")
    )
    moved = _moved(before, _snapshot("musubi_retrieval_warnings_total"))
    assert moved == {key: 1.0}, f"warnings must count once (deduped) at the boundary; got {moved}"


async def test_orchestration_counts_errors_once(monkeypatch: pytest.MonkeyPatch) -> None:
    before = _snapshot("musubi_retrieval_errors_total")
    result = await _run_orch(monkeypatch, Err(error=RetrievalError(kind="timeout", detail="t")))
    assert isinstance(result, Err)
    key = tuple({"kind": "timeout"}[n] for n in _labelnames("musubi_retrieval_errors_total"))
    moved = _moved(before, _snapshot("musubi_retrieval_errors_total"))
    assert moved == {key: 1.0}, f"errors must count once per failed request; got {moved}"


# --------------------------------------------------------------------------- #
# Blocker 3 — fail closed: unbounded / mismatched warnings never reach wire or metrics
# --------------------------------------------------------------------------- #


async def test_fail_closed_drops_non_allowlisted_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """An out-of-vocabulary code / free-text plane must be dropped BEFORE the envelope, wire, or a
    Prometheus label — otherwise a single degraded query could explode label cardinality."""
    before = _snapshot("musubi_retrieval_warnings_total")
    bad = RetrievalWarning(code="garbage; DROP TABLE", plane="not-a-plane")
    result = await _run_orch(monkeypatch, Ok(value=RetrievalEnvelope(results=[], warnings=(bad,))))
    assert isinstance(result, Ok)
    assert result.value.warnings == (), "non-allowlisted warning must not survive onto the envelope"
    assert _moved(before, _snapshot("musubi_retrieval_warnings_total")) == {}, (
        "a non-allowlisted warning must never become a metric label"
    )


async def test_fail_closed_drops_code_plane_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plane_timeout_<X> code whose structured plane is a DIFFERENT plane is inconsistent — fail
    closed (a smuggled mismatch must not reach wire/metrics)."""
    before = _snapshot("musubi_retrieval_warnings_total")
    mismatch = RetrievalWarning(code="plane_timeout_curated", plane="episodic")
    result = await _run_orch(
        monkeypatch, Ok(value=RetrievalEnvelope(results=[], warnings=(mismatch,)))
    )
    assert isinstance(result, Ok)
    assert result.value.warnings == ()
    assert _moved(before, _snapshot("musubi_retrieval_warnings_total")) == {}


# --------------------------------------------------------------------------- #
# Blocker 5 — /v1/context is counted at the shared boundary; /v1/retrieve does not double-count
# --------------------------------------------------------------------------- #


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
    monkeypatch.setattr("musubi.api.routers.context.authenticate_request", mock_auth)

    async def degraded_single(*args: Any, **kwargs: Any) -> Any:
        return Ok(value=RetrievalEnvelope(results=[], warnings=(plane_timeout("episodic"),)))

    monkeypatch.setattr("musubi.retrieve.orchestration._run_single", degraded_single)

    app = create_app(settings=api_settings)
    app.dependency_overrides[get_settings_dep] = lambda: api_settings
    app.dependency_overrides[get_qdrant_client] = lambda: None
    app.dependency_overrides[get_embedder] = lambda: None
    app.dependency_overrides[get_reranker] = lambda: None
    return TestClient(app)


def test_context_endpoint_counts_at_shared_boundary(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """/v1/context runs through the SAME orchestration boundary, so a degraded context request
    increments the warning counter (it is not a blind spot only /v1/retrieve covers)."""
    before = _snapshot("musubi_retrieval_warnings_total")
    client = _app(monkeypatch, api_settings)
    resp = client.post(
        "/v1/context",
        headers={"Authorization": "Bearer fake"},
        json={"namespace": "test/ns", "query_text": "q", "planes": ["episodic"]},
    )
    assert resp.status_code == 200, resp.text
    key = tuple(
        {"warning": "plane_timeout_episodic", "plane": "episodic"}[n]
        for n in _labelnames("musubi_retrieval_warnings_total")
    )
    assert _moved(before, _snapshot("musubi_retrieval_warnings_total")) == {key: 1.0}


def test_retrieve_endpoint_no_double_count(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """/v1/retrieve must count the degraded warning EXACTLY once (the boundary counts; the router does
    not also count) — no +2 from double accounting."""
    before = _snapshot("musubi_retrieval_warnings_total")
    client = _app(monkeypatch, api_settings)
    resp = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={"namespace": "test/ns/episodic", "query_text": "q", "mode": "deep"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == ["plane_timeout_episodic"]
    key = tuple(
        {"warning": "plane_timeout_episodic", "plane": "episodic"}[n]
        for n in _labelnames("musubi_retrieval_warnings_total")
    )
    assert _moved(before, _snapshot("musubi_retrieval_warnings_total")) == {key: 1.0}
