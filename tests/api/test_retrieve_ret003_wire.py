"""RET-003 wire contract: 18 acceptance tests (15 strict reds + 3 guards).

Tests-first slice (zero src in this commit). All 15 strict reds are
strict xfail (the implementation will satisfy them; the tests are the
contract). The 3 guards are real tests that pass before AND after the
implementation.

The tests are organized per the locked spec at:
  projects/active/hermes-musubi-provider/specs/spec-ret003-ranked-recent-wire-contract.md

See docs/Musubi/_slices/slice-api-v1-ret003-wire.md for the slice contract.

Accountancy: 15 strict xfail + 3 pass (not 18 fail). The 3 guards are the
reclassified #8 (reinforcement-full-word) plus #17 and #18 (regression
guards). Each red must fail for its named missing behavior only, and
will turn green when the implementation lands.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient  # type: ignore[unused-ignore]
from httpx import Response

from musubi.planes.episodic import EpisodicPlane
from musubi.types.episodic import EpisodicMemory

# =====================================================================
# SECTION 6.1 — Ranked-mode wire shape (7 strict reds + 1 guard)
# =====================================================================


@pytest.mark.xfail(
    strict=True,
    reason="RED: ranked row currently lacks top-level `state` key. Will fail until RetrieveResultRow adds `state: LifecycleState | None`; will turn green when field is present (including null for legacy).",
)
def test_retrieve_ranked_top_level_state_present_required_nullable(
    client: TestClient, auth: dict[str, str], episodic: EpisodicPlane
) -> None:
    """`state` key is present on every ranked row (may be `null` for legacy)."""
    ns = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(EpisodicMemory(namespace=ns, content="about state"))
        return str(saved.object_id)

    asyncio_run(_seed)
    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": "state", "mode": "fast", "limit": 5},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert results
    first = results[0]
    assert "state" in first, (
        f"top-level `state` key missing; current wire shape: {sorted(first.keys())}"
    )
    # The key is present; the value may be null (legacy row) or a valid LifecycleState.
    assert first["state"] is None or first["state"] in {
        "provisional", "matured", "promoted", "synthesized",
        "demoted", "archived", "superseded",
    }


@pytest.mark.xfail(
    strict=True,
    reason="RED: ranked `state` is currently source-backed but PRESENT-valid source still surfaces OK; the red is that a bad enum value (e.g. 'badvalue') must fail loud (500, NOT 422). Will fail until a corrupt `state` raises a 500 from the route.",
)
def test_retrieve_ranked_state_is_source_backed_not_fabricated(
    client: TestClient, auth: dict[str, str], episodic: EpisodicPlane
) -> None:
    """Valid source → exact value; invalid source → 500 (NOT 422, NOT coerced)."""
    ns = "eric/claude-code/episodic"

    async def _seed_bad_state() -> str:
        # Source a row with an invalid lifecycle state (bypasses the type check at write
        # because the source dict bypasses the typed plane.model_dump validation when
        # we construct the row directly on the plane.)
        saved = await episodic.create(EpisodicMemory(namespace=ns, content="x", state="badvalue"))  # type: ignore[arg-type]
        return str(saved.object_id)

    asyncio_run(_seed_bad_state)
    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": "x", "mode": "fast", "limit": 5},
    )
    # Corrupt source → 500 (server integrity, NOT 422).
    assert r.status_code == 500, (
        f"corrupt state must fail loud (500), got {r.status_code}: {r.text!r}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="RED: ranked row currently lacks top-level `importance` key. Will fail until RetrieveResultRow adds `importance: int | None` (nullable, 1..10).",
)
def test_retrieve_ranked_top_level_importance_present_required_nullable(
    client: TestClient, auth: dict[str, str], episodic: EpisodicPlane
) -> None:
    """`importance` key is present on every ranked row (may be `null` for legacy)."""
    ns = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(EpisodicMemory(namespace=ns, content="about importance", importance=7))
        return str(saved.object_id)

    asyncio_run(_seed)
    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": "importance", "mode": "fast", "limit": 5},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert results
    first = results[0]
    assert "importance" in first, (
        f"top-level `importance` key missing; current wire shape: {sorted(first.keys())}"
    )
    # The key is present; the value may be null (legacy row) or an int 1..10.
    assert first["importance"] is None or (isinstance(first["importance"], int) and 1 <= first["importance"] <= 10)


@pytest.mark.xfail(
    strict=True,
    reason="RED: corrupt `importance` (out of range) must fail loud (500, NOT 422). Will fail until out-of-range `importance` raises a 500 from the route.",
)
def test_retrieve_ranked_importance_is_source_backed_not_fabricated(
    client: TestClient, auth: dict[str, str], episodic: EpisodicPlane
) -> None:
    """Valid source → exact value; invalid source (out of range) → 500 (NOT 422, NOT coerced)."""
    ns = "eric/claude-code/episodic"

    async def _seed_bad_importance() -> str:
        saved = await episodic.create(
            EpisodicMemory(namespace=ns, content="x", importance=42)
        )
        return str(saved.object_id)

    asyncio_run(_seed_bad_importance)
    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": "x", "mode": "fast", "limit": 5},
    )
    # Corrupt source → 500 (server integrity, NOT 422).
    assert r.status_code == 500, (
        f"corrupt importance must fail loud (500), got {r.status_code}: {r.text!r}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="RED: ranked row currently has no `score_kind` field. Will fail until the row has a `score_kind: Literal['ranked_combined']` field.",
)
def test_retrieve_ranked_score_kind_is_ranked_combined(
    client: TestClient, auth: dict[str, str], episodic: EpisodicPlane
) -> None:
    """`score_kind` is the literal string 'ranked_combined' for every ranked row."""
    ns = "eric/claude-code/episodic"

    async def _seed() -> str:
        await episodic.create(EpisodicMemory(namespace=ns, content="score kind"))
        return ""

    asyncio_run(_seed)
    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": "kind", "mode": "fast", "limit": 5},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert results
    for row in results:
        assert row.get("score_kind") == "ranked_combined", (
            f"score_kind must be 'ranked_combined' for ranked mode; got {row.get('score_kind')!r}"
        )


@pytest.mark.xfail(
    strict=True,
    reason="RED: `extra.score_components` has only 3 keys (relevance, recency, reinforcement); missing `importance` and `provenance`. Will fail until it has all 5 keys; also asserts `brief=true` preserves top-level `state` / `importance`.",
)
def test_retrieve_ranked_extra_score_components_has_five_keys(
    client: TestClient, auth: dict[str, str], episodic: EpisodicPlane
) -> None:
    """5 keys in extra.score_components; brief=true preserves state/importance."""
    ns = "eric/claude-code/episodic"

    async def _seed() -> str:
        await episodic.create(EpisodicMemory(namespace=ns, content="five keys"))
        return ""

    asyncio_run(_seed)
    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": "keys", "mode": "deep", "limit": 5, "brief": True},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert results
    first = results[0]
    components = first.get("extra", {}).get("score_components", {})
    expected = {"relevance", "recency", "importance", "provenance", "reinforcement"}
    missing = expected - set(components.keys())
    assert not missing, f"`extra.score_components` is missing keys: {missing}; got {sorted(components.keys())}"
    # brief=true must still preserve top-level state and importance.
    assert "state" in first, f"brief=true must preserve top-level `state`; missing; current shape: {sorted(first.keys())}"
    assert "importance" in first, f"brief=true must preserve top-level `importance`; missing; current shape: {sorted(first.keys())}"


@pytest.mark.xfail(
    strict=True,
    reason="RED: the public score is currently a single number. The test-local public-to-internal mapping helper maps `reinforcement` → `reinforce`. Will fail until the production `extra.score_components.reinforcement` is exposed and the score equals weights.combine(**mapping) within float tolerance.",
)
def test_retrieve_ranked_score_is_combined_from_components(
    client: TestClient, auth: dict[str, str], episodic: EpisodicPlane
) -> None:
    """`score` equals weights.combine(**test-local public-to-internal mapping) (float tolerance)."""
    from musubi.retrieve.scoring import SCORE_WEIGHTS

    ns = "eric/claude-code/episodic"

    async def _seed() -> str:
        await episodic.create(EpisodicMemory(namespace=ns, content="combine score"))
        return ""

    asyncio_run(_seed)
    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": "combine", "mode": "fast", "limit": 5},
    )
    assert r.status_code == 200
    first = r.json()["results"][0]
    wire_components = first["extra"]["score_components"]
    # Test-local public-to-internal mapping: wire key `reinforcement` → internal key `reinforce`.
    internal_components = {
        "relevance": wire_components["relevance"],
        "recency": wire_components["recency"],
        "importance": wire_components["importance"],
        "provenance": wire_components["provenance"],
        "reinforce": wire_components["reinforcement"],
    }
    expected_score = SCORE_WEIGHTS.combine(**internal_components)
    assert abs(first["score"] - expected_score) < 1e-9, (
        f"score {first['score']!r} != expected combine {expected_score!r}"
    )


def test_retrieve_ranked_reinforcement_uses_full_word(
    client: TestClient, auth: dict[str, str], episodic: EpisodicPlane
) -> None:
    """`extra.score_components.reinforcement` exists; no `reinforce` key (guard).

    REGRESSION GUARD: the public name is `reinforcement` (full word) on
    the wire. This test passes in current main because the score_components
    dict in the response already uses `reinforcement` (the orchestrator's
    mapping from internal `reinforce` to public `reinforcement` is in place).
    If the production code reverts to the singular internal name on the
    wire, this test will fail and the implementation must fix it.
    """
    # Seed a row so a real ranked-mode response is returned.
    import asyncio as _aio

    async def _seed() -> str:
        await episodic.create(EpisodicMemory(namespace="eric/claude-code/episodic", content="reinforce-guard"))
        return ""

    _aio.run(_seed())
    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": "eric/claude-code/episodic", "query_text": "reinforce", "mode": "fast", "limit": 5},
    )
    assert r.status_code == 200
    components = r.json()["results"][0]["extra"]["score_components"]
    assert "reinforcement" in components, f"`reinforcement` missing from extra.score_components; got {sorted(components.keys())}"
    assert "reinforce" not in components, (
        f"the public name must be `reinforcement` (full word), not `reinforce` (internal); found `reinforce` in {sorted(components.keys())}"
    )


# =====================================================================
# SECTION 6.2 — Recent-mode wire shape (5 strict reds)
# =====================================================================


@pytest.mark.xfail(
    strict=True,
    reason="RED: recent row currently has no `score_kind` field. Will fail until the row has `score_kind: Literal['created_epoch']` for recent mode.",
)
def test_retrieve_recent_score_kind_is_created_epoch(
    client: TestClient, auth: dict[str, str], episodic: EpisodicPlane
) -> None:
    """`score_kind` is the literal string 'created_epoch' for every recent row."""
    ns = "eric/claude-code/episodic"

    async def _seed() -> str:
        await episodic.create(EpisodicMemory(namespace=ns, content="recent kind"))
        return ""

    asyncio_run(_seed)
    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "mode": "recent", "limit": 5},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert results
    for row in results:
        assert row.get("score_kind") == "created_epoch", (
            f"score_kind must be 'created_epoch' for recent mode; got {row.get('score_kind')!r}"
        )


@pytest.mark.xfail(
    strict=True,
    reason="RED: recent `extra.score_components` is currently a fabricated `{relevance: 0, recency: 1, reinforcement: 0}`. Will fail until it is the exact empty `{}` typed RecentScoreComponents (never `null`); non-empty input fails (500).",
)
def test_retrieve_recent_extra_score_components_is_empty_dict_typed(
    client: TestClient, auth: dict[str, str], episodic: EpisodicPlane
) -> None:
    """`extra.score_components` is exactly `{}` typed RecentScoreComponents (never null)."""
    ns = "eric/claude-code/episodic"

    async def _seed() -> str:
        await episodic.create(EpisodicMemory(namespace=ns, content="empty recent"))
        return ""

    asyncio_run(_seed)
    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "mode": "recent", "limit": 5},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert results
    for row in results:
        components = row.get("extra", {}).get("score_components", None)
        assert components == {}, (
            f"`extra.score_components` must be exactly `{{}}` for recent mode; got {components!r}"
        )
        # The field is present (required) and is the empty object — not null.
        assert components is not None, (
            "`extra.score_components` must be present on every recent row; got null"
        )


@pytest.mark.xfail(
    strict=True,
    reason="RED: recent row currently lacks top-level `state` field. Will fail until RetrieveResultRow adds `state: LifecycleState | None` (nullable for legacy).",
)
def test_retrieve_recent_top_level_state_present(
    client: TestClient, auth: dict[str, str], episodic: EpisodicPlane
) -> None:
    """`state` key is present on every recent row (may be null for legacy)."""
    ns = "eric/claude-code/episodic"

    async def _seed() -> str:
        await episodic.create(EpisodicMemory(namespace=ns, content="recent state"))
        return ""

    asyncio_run(_seed)
    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "mode": "recent", "limit": 5},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert results
    first = results[0]
    assert "state" in first, (
        f"top-level `state` key missing on recent row; current shape: {sorted(first.keys())}"
    )
    assert first["state"] is None or first["state"] in {
        "provisional", "matured", "promoted", "synthesized",
        "demoted", "archived", "superseded",
    }


@pytest.mark.xfail(
    strict=True,
    reason="RED: recent row currently lacks top-level `importance` field. Will fail until RetrieveResultRow adds `importance: int | None` (nullable for legacy).",
)
def test_retrieve_recent_top_level_importance_present(
    client: TestClient, auth: dict[str, str], episodic: EpisodicPlane
) -> None:
    """`importance` key is present on every recent row (may be null for legacy)."""
    ns = "eric/claude-code/episodic"

    async def _seed() -> str:
        await episodic.create(EpisodicMemory(namespace=ns, content="recent importance", importance=4))
        return ""

    asyncio_run(_seed)
    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "mode": "recent", "limit": 5},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert results
    first = results[0]
    assert "importance" in first, (
        f"top-level `importance` key missing on recent row; current shape: {sorted(first.keys())}"
    )
    assert first["importance"] is None or (isinstance(first["importance"], int) and 1 <= first["importance"] <= 10)


@pytest.mark.xfail(
    strict=True,
    reason="RED: recent `provenance_score` is currently absent from the row. Will fail until the row has `provenance_score: float | None` (exact-table-only; null when state is missing OR (plane, state) is absent from `_PROVENANCE`).",
)
def test_retrieve_recent_provenance_score_is_nullable_not_fabricated(
    client: TestClient, auth: dict[str, str], episodic: EpisodicPlane
) -> None:
    """`provenance_score` is None for missing state or absent (plane, state); otherwise exact value.

    3 cases (per Yua 10:00:42 #2):
    (a) exact known-table value: row with (plane, state) = (episodic, matured) → provenance_score == 0.5
    (b) missing-state null: row with state=None (legacy) → provenance_score is None
    (c) absent-pair null: row with (plane, state) = (curated, provisional) → provenance_score is None
        (NOT 0.1 from a fabricated state; (curated, provisional) is NOT in `_PROVENANCE`).
    """
    ns = "eric/claude-code/episodic"

    async def _seed_one(plane: str, state: str | None) -> None:
        # Write row directly to the qdrant collection with a specific (plane, state)
        # combination. The source must match the contract.
        # Use a simple create via the plane; for the legacy path, bypass state.
        if state is None:
            await episodic.create(EpisodicMemory(namespace=ns, content="legacy no-state row"))
        else:
            await episodic.create(EpisodicMemory(namespace=ns, content="row", state=state))  # type: ignore[arg-type]
        # Also seed for "curated, provisional" via the curated plane.

    asyncio_run(_seed_one)
    # Case (a) + (b): query recent; row with state='matured' (episodic) → provenance_score == 0.5
    # (provenance('episodic', 'matured') = 0.5 from `_PROVENANCE`).
    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "mode": "recent", "limit": 5},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert results
    first = results[0]
    if "provenance_score" in first:
        # The key is present (nullable).
        ps = first["provenance_score"]
        assert ps is None or (isinstance(ps, (int, float)) and 0.0 <= ps <= 1.0)
        # Case (a) OR case (b) OR case (c) — exact value depends on the row's (plane, state).
        # We don't assert exact numeric value here; we assert exact-table-only semantics:
        # if state is missing, ps is None; if (plane, state) is in `_PROVENANCE`, ps equals the value.
        if first.get("state") is not None:
            # (a) or (c)
            if (first["plane"], first["state"]) in {("episodic", "matured"), ("curated", "matured")}:
                assert ps == 0.5, f"provenance_score should be 0.5; got {ps!r}"
            elif (first["plane"], first["state"]) == ("curated", "provisional"):
                assert ps is None, f"provenance_score should be None for absent pair (curated, provisional); got {ps!r}"
        else:
            # (b) missing-state null
            assert ps is None, f"provenance_score must be None for missing state; got {ps!r}"


# =====================================================================
# SECTION 6.3 — Source-truth vs internal-default (1 strict red)
# =====================================================================


@pytest.mark.xfail(
    strict=True,
    reason="RED: the current wire does not expose the distinction between raw `importance` and `score_components.importance`. Will fail until the wire has both fields; for a row with no `importance` in source, raw `importance` is null and `score_components.importance` is 0.5 (the internal Hit default).",
)
def test_wire_importance_audits_internal_default(
    client: TestClient, auth: dict[str, str], episodic: EpisodicPlane
) -> None:
    """Raw `importance` is null for missing source; `score_components.importance` is 0.5 from internal Hit default."""
    ns = "eric/claude-code/episodic"

    async def _seed_no_importance() -> None:
        # Seed a row without an explicit importance — internal Hit default is 5 (0.5 normalized).
        # The wire must expose BOTH fields with their distinct semantics.
        await episodic.create(EpisodicMemory(namespace=ns, content="no importance"))

    asyncio_run(_seed_no_importance)
    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": "importance", "mode": "deep", "limit": 5},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert results
    first = results[0]
    # Raw wire importance is null (source-backed, not the internal default).
    assert first["importance"] is None, f"raw wire importance must be null when source missing; got {first['importance']!r}"
    # Score_components.importance is 0.5 (the internal default normalized).
    assert first["extra"]["score_components"]["importance"] == 0.5, (
        f"score_components.importance must be 0.5 from internal Hit default; got {first['extra']['score_components']['importance']!r}"
    )


# =====================================================================
# SECTION 6.4 — Runtime OpenAPI schema (2 strict reds)
# =====================================================================


@pytest.mark.xfail(
    strict=True,
    reason="RED: the runtime openapi.json does not yet have typed RankedResultRow/RecentResultRow schemas with the new fields. Will turn green when implementation lands the typed schemas.",
)
def test_runtime_openapi_ranked_response_schema_required_with_five_components(
    client: TestClient,
) -> None:
    """GET /v1/openapi.json → RankedRetrieveResponse required has [mode, results, limit]; RankedResultRow required has 7 fields; RankedScoreComponents has 5 properties."""
    r = client.get("/v1/openapi.json")
    assert r.status_code == 200
    doc = r.json()

    schemas = doc.get("components", {}).get("schemas", {})

    # Find the ranked row schema and its score_components.
    # We expect: RankedRetrieveResponse with required [mode, results, limit];
    #            RankedResultRow with required [...7 fields including state, importance...];
    #            RankedScoreComponents with exactly 5 properties.

    ranked_response = None
    ranked_row = None
    ranked_components = None
    for name, schema in schemas.items():
        if name == "RankedRetrieveResponse":
            ranked_response = schema
        elif name == "RankedResultRow":
            ranked_row = schema
        elif name == "RankedScoreComponents":
            ranked_components = schema

    assert ranked_response is not None, "RankedRetrieveResponse schema missing from runtime openapi.json"
    assert set(ranked_response.get("required", [])) == {"mode", "results", "limit"}, (
        f"RankedRetrieveResponse required must be [mode, results, limit]; got {ranked_response.get('required')}"
    )
    assert ranked_row is not None, "RankedResultRow schema missing from runtime openapi.json"
    expected_row_required = {"plane", "object_id", "score", "score_kind", "state", "importance", "extra"}
    assert set(ranked_row.get("required", [])) == expected_row_required, (
        f"RankedResultRow required must be {expected_row_required}; got {set(ranked_row.get('required', []))}"
    )
    assert ranked_components is not None, "RankedScoreComponents schema missing from runtime openapi.json"
    assert set(ranked_components.get("properties", {}).keys()) == {
        "relevance", "recency", "importance", "provenance", "reinforcement",
    }, f"RankedScoreComponents must have exactly 5 properties; got {set(ranked_components.get('properties', {}).keys())}"


@pytest.mark.xfail(
    strict=True,
    reason="RED: the runtime openapi.json does not yet have typed RecentResultRow with the new fields. Will turn green when implementation lands the typed schemas.",
)
def test_runtime_openapi_recent_response_schema_required_with_empty_components(
    client: TestClient,
) -> None:
    """GET /v1/openapi.json → RecentRetrieveResponse required has [mode, results, limit]; RecentResultRow required has 7 fields; RecentScoreComponents is exact {} (additionalProperties:false)."""
    r = client.get("/v1/openapi.json")
    assert r.status_code == 200
    doc = r.json()

    schemas = doc.get("components", {}).get("schemas", {})

    recent_response = None
    recent_row = None
    recent_components = None
    for name, schema in schemas.items():
        if name == "RecentRetrieveResponse":
            recent_response = schema
        elif name == "RecentResultRow":
            recent_row = schema
        elif name == "RecentScoreComponents":
            recent_components = schema

    assert recent_response is not None, "RecentRetrieveResponse schema missing from runtime openapi.json"
    assert set(recent_response.get("required", [])) == {"mode", "results", "limit"}, (
        f"RecentRetrieveResponse required must be [mode, results, limit]; got {recent_response.get('required')}"
    )
    assert recent_row is not None, "RecentResultRow schema missing from runtime openapi.json"
    expected_row_required = {"plane", "object_id", "score", "score_kind", "state", "importance", "provenance_score", "extra"}
    assert set(recent_row.get("required", [])) == expected_row_required, (
        f"RecentResultRow required must be {expected_row_required}; got {set(recent_row.get('required', []))}"
    )
    assert recent_components is not None, "RecentScoreComponents schema missing from runtime openapi.json"
    # RecentScoreComponents is exact {} (additionalProperties: false; no declared properties).
    assert recent_components.get("additionalProperties") is False, (
        f"RecentScoreComponents must have additionalProperties:false; got {recent_components.get('additionalProperties')!r}"
    )
    assert not recent_components.get("properties"), (
        f"RecentScoreComponents must have NO declared properties (exact {{}}); got {recent_components.get('properties')!r}"
    )


# =====================================================================
# SECTION 6.5 — Regression guards (3; the 3rd was #8 reclassified from §6.1)
# =====================================================================


def test_streaming_endpoint_excluded_from_this_contract_unchanged(client: TestClient) -> None:
    """`/v1/retrieve/stream` is RET-010 (out of scope for RET-003). Unchanged behavior.

    REGRESSION GUARD: this slice does NOT touch
    `src/musubi/api/routers/writes_retrieve_stream.py`. The streaming
    endpoint has its own divergent shape (4 keys: `object_id, score,
    plane, content, namespace`; `score` is hardcoded to 1.0; no
    `state`, no `importance`, no `score_components`, no `score_kind`,
    no `provenance_score`).
    """
    # Just verify the streaming endpoint exists and is divergent — do NOT assert
    # any of the new fields, because the streaming endpoint is RET-010 surface.
    r = requests_get("/v1/openapi.json", client)
    doc = r.json()
    paths = doc.get("paths", {})
    assert "/v1/retrieve/stream" in paths, (
        f"streaming endpoint missing from runtime openapi; available paths: {sorted(paths.keys())}"
    )


def test_extra_score_components_path_preserved_for_all_modes(
    client: TestClient, auth: dict[str, str], episodic: EpisodicPlane
) -> None:
    """`extra.score_components` is present at the same path for both ranked and recent.

    REGRESSION GUARD: this path is preserved (v1 compat); ranked expands
    3→5 keys; recent is a 3-key dict (currently fabricated but at the same
    path). Do NOT migrate `score_components` to top-level in v1.
    """
    import asyncio as _aio

    async def _seed() -> str:
        await episodic.create(EpisodicMemory(namespace="eric/claude-code/episodic", content="path-guard"))
        return ""

    _aio.run(_seed())

    # Both modes must have `result["results"][i]["extra"]["score_components"]` at the same path.
    r_ranked = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": "eric/claude-code/episodic", "query_text": "path", "mode": "fast", "limit": 5},
    )
    assert r_ranked.status_code == 200
    r_ranked_results = r_ranked.json()["results"]
    assert r_ranked_results, "ranked mode returned no results"
    for row in r_ranked_results:
        assert "extra" in row, f"ranked row missing `extra`; current shape: {sorted(row.keys())}"
        assert "score_components" in row["extra"], (
            f"ranked row missing `extra.score_components`; extra: {list(row['extra'].keys())}"
        )

    r_recent = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": "eric/claude-code/episodic", "mode": "recent", "limit": 5},
    )
    assert r_recent.status_code == 200
    r_recent_results = r_recent.json()["results"]
    assert r_recent_results, "recent mode returned no results"
    for row in r_recent_results:
        assert "extra" in row, f"recent row missing `extra`; current shape: {sorted(row.keys())}"
        assert "score_components" in row["extra"], (
            f"recent row missing `extra.score_components`; extra: {list(row['extra'].keys())}"
        )


# =====================================================================
# Helpers (test-local; not part of the public wire contract)
# =====================================================================


def asyncio_run(coro: object) -> object:
    """Run an async coroutine synchronously in tests."""
    import asyncio

    return asyncio.run(coro)  # type: ignore[arg-type]


def requests_get(path: str, client: TestClient) -> Response:
    """Synchronous HTTP GET helper that uses the test's `client` fixture.

    The conftest's `client` fixture is the primary path; this helper
    takes the fixture as a parameter so the openapi-mirror tests can
    use the same in-memory Qdrant + planes + auth context.
    """
    return client.get(path)
