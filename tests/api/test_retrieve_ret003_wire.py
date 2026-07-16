"""RET-003 wire contract: 19 acceptance tests (19 + 3 guards; all 19 are GREEN with the implementation in).

Tests-first slice (zero src in this commit). All 16 strict reds are
strict xfail (the implementation will satisfy them; the tests are the
contract). The 3 guards are real tests that pass before AND after the
implementation.

The tests are organized per the locked spec at:
  projects/active/hermes-musubi-provider/specs/spec-ret003-ranked-recent-wire-contract.md

See docs/Musubi/_slices/slice-api-v1-ret003-wire.md for the slice contract.

Accountancy: 19 pass + 3 guards = 22 acceptance tests (was 15 + 3 = 18 at the tests-first head; +1 from the Yua #2 split of test 13, +3 from Yua #4/#5/#6).
The 3 guards are the reclassified #8 (reinforcement-full-word) plus
#18 and #19 (regression guards). The count is 19 because test #13
(provenance_score) is split per Yua 2026-07-13 11:57:59 #2 into:

  #13a HTTP exact cases (episodic,matured)=0.5 and (curated,provisional)=None
  #13b Orchestration->wire projection seam case (state=None → provenance_score=None)

Each red must fail for its named missing behavior only, and will turn
green when the implementation lands. ``pytest --runxfail`` is the
honest evidence boundary: it runs every xfail and verifies the test
fails at its named assertion (not at setup or row-selection). This is
not the same as bounded discrimination helpers (which would require
mutating the production source); it is the floor the tests-first slice
commits to.

Source-row discipline (Yua 2026-07-13 11:03:25 + 11:57:59 corrections):

- B1: tests that exercise "corrupt source" behavior seed via RAW qdrant.upsert, NOT through the typed
  EpisodicMemory model (which would reject the bad value client-side so the corrupt source never reaches
  the store). We bypass validation and assert the corrupt point is present at the store before exercising
  the route.
  **DATA-001 P2 supersession (2026-07-16):** the earlier B1 contract demanded HTTP 500 on a corrupt-source
  RANKED read. The accepted DATA-001 P2 ADR (data001-phase2-immutable-vectors) rules that a malformed
  RANKED candidate is SKIPPED (fail closed), never 500-ing the whole retrieval over one bad row — so the
  corrupt-state and corrupt-importance ranked tests now assert **200 with the bad row OMITTED** (never
  fabricated or exposed). This supersedes B1 fail-loud FOR RANKED READS ONLY; IDENTITY reads (scan /
  vault-path resolution) remain fail-loud. See ADR 0035 §DATA-001 P2 supersession.

- B2: tests that exercise "no importance in source" behavior (raw
  wire `importance` should be null when source is missing) seed via
  RAW qdrant.upsert with the importance key ABSENT, NOT through the
  typed EpisodicMemory (which writes the model default of 5).

- B3: tests that exercise recent-mode state-bearing behavior MUST
  seed a state value IN the recent mode's mandatory state filter
  (``provisional``, ``matured``, ``promoted``). Otherwise the
  orchestrator's `run_recent_retrieve` filter excludes the row and
  the red fails at "row is None" instead of its named assertion.

- B4: across all behavioral tests, stop trusting results[0]. Each
  test captures the seeded object_id and locates that exact returned
  row; otherwise an older candidate can satisfy or fail the wrong
  claim.

- B5: test #13's missing-state case can never traverse canonical
  recent HTTP because recent's state filter excludes rows with
  state=None. That case is tested at the orchestration->wire
  projection seam: a row-factory seam constructs a ``RetrievalResult``
  with state=None directly and asserts the wire projection emits
  ``provenance_score=None``. The HTTP-exact cases (a) and (c) still
  traverse canonical recent HTTP.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from qdrant_client import QdrantClient, models

from musubi.api.responses import (
    RankedExtra,
    RankedResultRow,
    RankedScoreComponents,
    RecentExtra,
    RecentResultRow,
    RecentScoreComponents,
)
from musubi.planes.episodic.plane import episodic_point_id
from musubi.store.names import collection_for_plane
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.common import generate_ksuid

# =====================================================================
# Helpers (test-local; not part of the public wire contract)
# =====================================================================


def _marker() -> str:
    """Return a unique marker so a test can find its exact row in the response."""
    return f"mark-{uuid.uuid4().hex[:12]}"


def _raw_upsert(
    qdrant: QdrantClient,
    *,
    collection: str,
    object_id: str,
    payload: dict[str, object],
    marker_in_content: str | None = None,
) -> str:
    """Seed a point by RAW Qdrant upsert, bypassing typed model validation.

    Returns the object_id (so the test can locate the exact row by id).
    The marker_in_content is written into both `content` and `summary`
    so the test can locate the row by content match if the response
    doesn't expose object_id (it currently does not; ranked-mode rows
    expose object_id, recent-mode rows do not).
    """
    if marker_in_content is not None:
        # Spread the marker into the fields the orchestrator surfaces.
        payload = dict(payload)
        payload["content"] = f"{marker_in_content} :: {payload.get('content', '')}".strip()
        if "summary" not in payload or not payload["summary"]:
            payload["summary"] = marker_in_content
    payload = dict(payload)
    payload.setdefault("object_id", object_id)
    payload.setdefault("namespace", "eric/claude-code/episodic")
    # Recent-mode Qdrant scroll orders by `created_epoch` DESC; rows missing
    # this field are filtered out by the order_by path. Seed it explicitly
    # so recent-mode tests find the row.
    if "created_epoch" not in payload or payload["created_epoch"] is None:
        payload["created_epoch"] = time.time()
    # In-memory Qdrant FakeEmbedder: dense = 1024 zeros, sparse = empty.
    # The Qdrant point ID is the deterministic UUID derived from the KSUID
    # via :func:`episodic_point_id` — the same translation the production
    # plane uses, so the in-memory Qdrant accepts the upsert.
    point = models.PointStruct(
        id=episodic_point_id(object_id),
        payload=payload,
        vector={
            DENSE_VECTOR_NAME: [0.0] * 1024,
            SPARSE_VECTOR_NAME: models.SparseVector(indices=[0], values=[0.0]),
        },
    )
    qdrant.upsert(collection_name=collection, points=[point])
    return object_id


def _find_row_by_object_id(results: list[dict[str, Any]], object_id: str) -> dict[str, Any] | None:
    """Locate the exact returned row whose object_id matches the seed."""
    for row in results:
        if row.get("object_id") == object_id:
            return row
    return None


def _find_row_by_marker(results: list[dict[str, Any]], marker: str) -> dict[str, Any] | None:
    """Locate the exact returned row whose content contains the marker."""
    for row in results:
        content = row.get("content") or row.get("summary") or ""
        if marker in str(content):
            return row
    return None


def asyncio_run(coro: object) -> object:
    """Run an async coroutine synchronously in tests."""
    import asyncio

    return asyncio.run(coro)  # type: ignore[arg-type]


def requests_get(path: str, client: TestClient) -> Response:
    """Synchronous HTTP GET helper that uses the test's `client` fixture."""
    return cast(Response, client.get(path))


# =====================================================================
# SECTION 6.1 — Ranked-mode wire shape (7 strict reds + 1 guard)
# =====================================================================


def test_retrieve_ranked_top_level_state_present_required_nullable(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """`state` key is present on every ranked row (may be `null` for legacy)."""
    ns = "eric/claude-code/episodic"
    marker = _marker()
    object_id = str(generate_ksuid())

    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=object_id,
        payload={
            "namespace": ns,
            "object_id": object_id,
            "state": "matured",
        },
        marker_in_content=marker,
    )

    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": marker, "mode": "fast", "limit": 10},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    row = _find_row_by_object_id(results, object_id)
    assert row is not None, (
        f"seeded row {object_id} not returned; results object_ids: {[r.get('object_id') for r in results]}"
    )
    assert "state" in row, (
        f"top-level `state` key missing; current wire shape: {sorted(row.keys())}"
    )
    # The key is present; the value may be null (legacy row) or a valid LifecycleState.
    assert row["state"] is None or row["state"] in {
        "provisional",
        "matured",
        "promoted",
        "synthesized",
        "demoted",
        "archived",
        "superseded",
    }, f"state value not in LifecycleState; got {row['state']!r}"


def test_retrieve_ranked_state_is_source_backed_not_fabricated(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """A corrupt-source row (raw upsert with a bad ``state`` enum) is OMITTED from a ranked read, never
    fabricated or exposed — and the query still returns 200.

    DATA-001 P2 (accepted ADR) supersedes the RET-003 B1 fail-loud contract FOR RANKED READS: a malformed
    ranked candidate is skipped (fail closed), never 500-ing the whole retrieval over one bad row. (Identity
    reads — scan/vault-path — remain fail-loud; that is unchanged.) Seeded via RAW qdrant.upsert to bypass
    typed validation; the corrupt point is asserted present at the store, then proven absent from results.
    """
    ns = "eric/claude-code/episodic"
    object_id = str(generate_ksuid())

    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=object_id,
        payload={
            "namespace": ns,
            "object_id": object_id,
            "state": "badvalue",  # NOT in LifecycleState; bypasses typed validation
        },
    )

    # Prove the corrupt source is present at the store (not filtered by Pydantic).
    raw_point = qdrant.retrieve(
        collection_name=collection_for_plane("episodic"),
        ids=[episodic_point_id(object_id)],
        with_payload=True,
    )
    assert raw_point and raw_point[0].payload is not None, "corrupt point not present in store"
    assert raw_point[0].payload.get("state") == "badvalue", (
        f"corrupt state not seeded; got {raw_point[0].payload.get('state')!r}"
    )

    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": "x", "mode": "fast", "limit": 5},
    )
    # DATA-001 P2: 200 with the malformed row skipped (fail closed), NOT a 500.
    assert r.status_code == 200, (
        f"a malformed ranked candidate is skipped, not 500 (DATA-001 P2); got {r.status_code}: {r.text!r}"
    )
    ids = [row.get("object_id") for row in r.json()["results"]]
    assert object_id not in ids, (
        "the corrupt-state row must be omitted, never fabricated or exposed"
    )


def test_retrieve_ranked_top_level_importance_present_required_nullable(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """`importance` key is present on every ranked row (may be `null` for legacy)."""
    ns = "eric/claude-code/episodic"
    marker = _marker()
    object_id = str(generate_ksuid())

    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=object_id,
        payload={
            "namespace": ns,
            "object_id": object_id,
            "importance": 7,
            "state": "matured",  # DATA-001 P2: state is now filtered post-hydration; seed it visible
        },
        marker_in_content=marker,
    )

    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": marker, "mode": "fast", "limit": 10},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    row = _find_row_by_object_id(results, object_id)
    assert row is not None, (
        f"seeded row {object_id} not returned; results object_ids: {[r.get('object_id') for r in results]}"
    )
    assert "importance" in row, (
        f"top-level `importance` key missing; current wire shape: {sorted(row.keys())}"
    )
    # The key is present; the value may be null (legacy row) or an int 1..10.
    assert row["importance"] is None or (
        isinstance(row["importance"], int) and 1 <= row["importance"] <= 10
    ), f"importance not in 1..10 or null; got {row['importance']!r}"


def test_retrieve_ranked_importance_is_source_backed_not_fabricated(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """A corrupt-source row (raw upsert with out-of-range ``importance``) is OMITTED from a ranked read,
    never fabricated or exposed — and the query still returns 200.

    DATA-001 P2 (accepted ADR) supersedes the RET-003 B1 fail-loud contract FOR RANKED READS: a malformed
    ranked candidate is skipped (fail closed), never 500-ing the whole retrieval. (Identity reads remain
    fail-loud.) Seeded via RAW qdrant.upsert to bypass typed validation.
    """
    ns = "eric/claude-code/episodic"
    object_id = str(generate_ksuid())

    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=object_id,
        payload={
            "namespace": ns,
            "object_id": object_id,
            "importance": 42,  # out of 1..10 range
        },
    )

    # Prove the corrupt source is present at the store.
    raw_point = qdrant.retrieve(
        collection_name=collection_for_plane("episodic"),
        ids=[episodic_point_id(object_id)],
        with_payload=True,
    )
    assert raw_point and raw_point[0].payload is not None, "corrupt point not present in store"
    assert raw_point[0].payload.get("importance") == 42, (
        f"corrupt importance not seeded; got {raw_point[0].payload.get('importance')!r}"
    )

    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": "x", "mode": "fast", "limit": 5},
    )
    # DATA-001 P2: 200 with the malformed row skipped (fail closed), NOT a 500.
    assert r.status_code == 200, (
        f"a malformed ranked candidate is skipped, not 500 (DATA-001 P2); got {r.status_code}: {r.text!r}"
    )
    ids = [row.get("object_id") for row in r.json()["results"]]
    assert object_id not in ids, "the out-of-range-importance row must be omitted, never exposed"


def test_retrieve_ranked_score_kind_is_ranked_combined(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """`score_kind` is the literal string 'ranked_combined' for every ranked row."""
    ns = "eric/claude-code/episodic"
    marker = _marker()
    object_id = str(generate_ksuid())

    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=object_id,
        payload={"namespace": ns, "object_id": object_id, "state": "matured"},  # P2: seed visible
        marker_in_content=marker,
    )

    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": marker, "mode": "fast", "limit": 10},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    row = _find_row_by_object_id(results, object_id)
    assert row is not None, (
        f"seeded row {object_id} not returned; results object_ids: {[r.get('object_id') for r in results]}"
    )
    assert row.get("score_kind") == "ranked_combined", (
        f"score_kind must be 'ranked_combined' for ranked mode; got {row.get('score_kind')!r}"
    )


def test_retrieve_ranked_extra_score_components_has_five_keys(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """5 keys in extra.score_components; brief=true preserves state/importance."""
    ns = "eric/claude-code/episodic"
    marker = _marker()
    object_id = str(generate_ksuid())

    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=object_id,
        payload={
            "namespace": ns,
            "object_id": object_id,
            "state": "matured",
            "importance": 7,
        },
        marker_in_content=marker,
    )

    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": marker, "mode": "deep", "limit": 10, "brief": True},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    row = _find_row_by_object_id(results, object_id)
    assert row is not None, (
        f"seeded row {object_id} not returned; results object_ids: {[r.get('object_id') for r in results]}"
    )
    components = row.get("extra", {}).get("score_components", {})
    expected = {"relevance", "recency", "importance", "provenance", "reinforcement"}
    missing = expected - set(components.keys())
    assert not missing, (
        f"`extra.score_components` is missing keys: {missing}; got {sorted(components.keys())}"
    )
    # brief=true must still preserve top-level state and importance.
    assert "state" in row, (
        f"brief=true must preserve top-level `state`; missing; current shape: {sorted(row.keys())}"
    )
    assert "importance" in row, (
        f"brief=true must preserve top-level `importance`; missing; current shape: {sorted(row.keys())}"
    )


def test_retrieve_ranked_score_is_combined_from_components(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """`score` equals weights.combine(**test-local public-to-internal mapping) (float tolerance)."""
    from musubi.retrieve.scoring import SCORE_WEIGHTS

    ns = "eric/claude-code/episodic"
    marker = _marker()
    object_id = str(generate_ksuid())

    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=object_id,
        payload={"namespace": ns, "object_id": object_id, "state": "matured"},  # P2: seed visible
        marker_in_content=marker,
    )

    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": marker, "mode": "fast", "limit": 10},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    row = _find_row_by_object_id(results, object_id)
    assert row is not None, (
        f"seeded row {object_id} not returned; results object_ids: {[r.get('object_id') for r in results]}"
    )
    wire_components = row["extra"]["score_components"]
    # Test-local public-to-internal mapping: wire key `reinforcement` → internal key `reinforce`.
    internal_components = {
        "relevance": wire_components["relevance"],
        "recency": wire_components["recency"],
        "importance": wire_components["importance"],
        "provenance": wire_components["provenance"],
        "reinforce": wire_components["reinforcement"],
    }
    expected_score = SCORE_WEIGHTS.combine(**internal_components)
    assert abs(row["score"] - expected_score) < 1e-9, (
        f"score {row['score']!r} != expected combine {expected_score!r}"
    )


def test_retrieve_ranked_reinforcement_uses_full_word(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """`extra.score_components.reinforcement` exists; no `reinforce` key (guard).

    REGRESSION GUARD: the public name is `reinforcement` (full word) on
    the wire. This test passes in current main because the score_components
    dict in the response already uses `reinforcement` (the orchestrator's
    mapping from internal `reinforce` to public `reinforcement` is in place).
    If the production code reverts to the singular internal name on the
    wire, this test will fail and the implementation must fix it.
    """
    ns = "eric/claude-code/episodic"
    marker = _marker()
    object_id = str(generate_ksuid())

    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=object_id,
        payload={"namespace": ns, "object_id": object_id, "state": "matured"},  # P2: seed visible
        marker_in_content=marker,
    )

    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": marker, "mode": "fast", "limit": 10},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    row = _find_row_by_object_id(results, object_id)
    assert row is not None, (
        f"seeded row {object_id} not returned; results object_ids: {[r.get('object_id') for r in results]}"
    )
    components = row["extra"]["score_components"]
    assert "reinforcement" in components, (
        f"`reinforcement` missing from extra.score_components; got {sorted(components.keys())}"
    )
    assert "reinforce" not in components, (
        f"the public name must be `reinforcement` (full word), not `reinforce` (internal); found `reinforce` in {sorted(components.keys())}"
    )


# =====================================================================
# SECTION 6.2 — Recent-mode wire shape (5 strict reds)
# =====================================================================


def test_retrieve_recent_score_kind_is_created_epoch(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """`score_kind` is the literal string 'created_epoch' for every recent row."""
    ns = "eric/claude-code/episodic"
    marker = _marker()
    object_id = str(generate_ksuid())

    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=object_id,
        payload={"namespace": ns, "object_id": object_id, "state": "provisional"},
        marker_in_content=marker,
    )

    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "mode": "recent", "limit": 10},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    row = _find_row_by_object_id(results, object_id)
    assert row is not None, (
        f"seeded row {object_id} not returned; results object_ids: {[r.get('object_id') for r in results]}"
    )
    assert row.get("score_kind") == "created_epoch", (
        f"score_kind must be 'created_epoch' for recent mode; got {row.get('score_kind')!r}"
    )


def test_retrieve_recent_extra_score_components_is_empty_dict_typed(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """`extra.score_components` is exactly `{}` typed RecentScoreComponents (never null)."""
    ns = "eric/claude-code/episodic"
    marker = _marker()
    object_id = str(generate_ksuid())

    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=object_id,
        payload={"namespace": ns, "object_id": object_id, "state": "provisional"},
        marker_in_content=marker,
    )

    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "mode": "recent", "limit": 10},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    row = _find_row_by_object_id(results, object_id)
    assert row is not None, (
        f"seeded row {object_id} not returned; results object_ids: {[r.get('object_id') for r in results]}"
    )
    components = row.get("extra", {}).get("score_components", None)
    assert components == {}, (
        f"`extra.score_components` must be exactly `{{}}` for recent mode; got {components!r}"
    )
    # The field is present (required) and is the empty object — not null.
    assert components is not None, (
        "`extra.score_components` must be present on every recent row; got null"
    )


def test_retrieve_recent_top_level_state_present(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """`state` key is present on every recent row (may be null for legacy)."""
    ns = "eric/claude-code/episodic"
    marker = _marker()
    object_id = str(generate_ksuid())

    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=object_id,
        payload={"namespace": ns, "object_id": object_id, "state": "matured"},
        marker_in_content=marker,
    )

    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "mode": "recent", "limit": 10},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    row = _find_row_by_object_id(results, object_id)
    assert row is not None, (
        f"seeded row {object_id} not returned; results object_ids: {[r.get('object_id') for r in results]}"
    )
    assert "state" in row, (
        f"top-level `state` key missing on recent row; current shape: {sorted(row.keys())}"
    )
    assert row["state"] is None or row["state"] in {
        "provisional",
        "matured",
        "promoted",
        "synthesized",
        "demoted",
        "archived",
        "superseded",
    }, f"state value not in LifecycleState; got {row['state']!r}"


def test_retrieve_recent_top_level_importance_present(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """`importance` key is present on every recent row (may be null for legacy).

    Yua 2026-07-13 11:57:59 #1: seed state=provisional explicitly so the
    row is NOT dropped by recent's mandatory state filter
    (provisional, matured, promoted). The red must fail at the named
    "importance key present" assertion, not at the row-selection
    boundary.
    """
    ns = "eric/claude-code/episodic"
    marker = _marker()
    object_id = str(generate_ksuid())

    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=object_id,
        payload={
            "namespace": ns,
            "object_id": object_id,
            "state": "provisional",
            "importance": 4,
        },
        marker_in_content=marker,
    )

    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "mode": "recent", "limit": 10},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    row = _find_row_by_object_id(results, object_id)
    assert row is not None, (
        f"seeded row {object_id} not returned; results object_ids: {[r.get('object_id') for r in results]}"
    )
    assert "importance" in row, (
        f"top-level `importance` key missing on recent row; current shape: {sorted(row.keys())}"
    )
    assert row["importance"] is None or (
        isinstance(row["importance"], int) and 1 <= row["importance"] <= 10
    ), f"importance not in 1..10 or null; got {row['importance']!r}"


def test_retrieve_recent_provenance_score_http_exact(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """HTTP exact: `provenance_score` is the table value for known (plane, state) pairs; None for absent pairs.

    TWO distinct raw rows are seeded (per Yua 11:57:59 #2 — the missing-state
    case (b) moved to the projection-seam test because canonical recent
    Qdrant never returns state=None). Each row has a unique marker; the
    test locates each row in the response and asserts the EXACT
    provenance_score for that (plane, state) pair.

    Cases (HTTP exact only — both rows are in the recent default state
    filter so they traverse canonical recent HTTP):
    (a) episodic + matured → provenance_score == 0.5 (in `_PROVENANCE`)
    (c) curated + provisional → provenance_score is None (NOT 0.1 from a
        fabricated state; (curated, provisional) is NOT in `_PROVENANCE`)
    """
    ns_episodic = "eric/claude-code/episodic"
    ns_curated = "eric/claude-code/curated"

    marker_a = _marker()
    marker_c = _marker()
    oid_a = str(generate_ksuid())
    oid_c = str(generate_ksuid())

    # (a) episodic + matured (in `_PROVENANCE`; expected 0.5)
    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=oid_a,
        payload={
            "namespace": ns_episodic,
            "object_id": oid_a,
            "state": "matured",
        },
        marker_in_content=marker_a,
    )
    # (c) curated + provisional (NOT in `_PROVENANCE`; expected None)
    _raw_upsert(
        qdrant,
        collection=collection_for_plane("curated"),
        object_id=oid_c,
        payload={
            "namespace": ns_curated,
            "object_id": oid_c,
            "state": "provisional",
        },
        marker_in_content=marker_c,
    )

    # Query recent for each namespace separately, then locate the exact row by id.
    r_epi = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns_episodic, "mode": "recent", "limit": 20},
    )
    assert r_epi.status_code == 200
    epi_results = r_epi.json()["results"]
    row_a = _find_row_by_object_id(epi_results, oid_a)
    assert row_a is not None, f"(a) row {oid_a} not in episodic recent results"

    r_cur = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns_curated, "mode": "recent", "limit": 20},
    )
    assert r_cur.status_code == 200
    cur_results = r_cur.json()["results"]
    row_c = _find_row_by_object_id(cur_results, oid_c)
    assert row_c is not None, f"(c) row {oid_c} not in curated recent results"

    # (a) episodic + matured → provenance_score == 0.5
    assert "provenance_score" in row_a, (
        f"(a) provenance_score key missing on row; current shape: {sorted(row_a.keys())}"
    )
    assert row_a["provenance_score"] == 0.5, (
        f"(a) provenance_score should be 0.5 for (episodic, matured); got {row_a['provenance_score']!r}"
    )
    # (c) curated + provisional → provenance_score is None (not 0.1 from fabrication)
    assert "provenance_score" in row_c, (
        f"(c) provenance_score key missing on row; current shape: {sorted(row_c.keys())}"
    )
    assert row_c["provenance_score"] is None, (
        f"(c) provenance_score must be None for (curated, provisional) NOT in _PROVENANCE; "
        f"got {row_c['provenance_score']!r}"
    )


def test_retrieve_recent_provenance_score_seam_state_none() -> None:
    """Orchestration->wire projection seam: state=None → provenance_score=None.

    Per Yua 2026-07-13 11:57:59 #2, the missing-state case (b) cannot be
    exercised through canonical recent HTTP because
    ``run_recent_retrieve`` always filters state to
    (provisional, matured, promoted). A row with state=None would be
    silently dropped before reaching the wire.

    The seam is the row-factory function that the orchestrator's
    recent branch uses to project a `RecentHit` (or the equivalent
    internal row representation) into a wire row. This test
    constructs a row object with state=None directly and asserts the
    projection emits `provenance_score=None` (exact-table-only — no
    fabricated 0.1 default).

    The seam function is a stable named test-local handle to the
    production projection; the test is RED today because the
    projection does not yet expose the row-factory seam.
    """
    # The seam signature is: project(plane: str, state: str | None) -> float | None
    # (no fabrication; exact-table-only; None for state=None or
    # absent (plane, state) pair). The implementation MUST expose a
    # named row-factory seam so callers can construct internal rows
    # with state=None and verify the wire emits provenance_score=None.
    #
    # Today no such seam exists; this test is xfail for the named
    # missing behavior. Implementation acceptance: production exposes
    # a stable seam (e.g. ``musubi.retrieve.recent._provenance_score_for``)
    # that returns None for state=None.
    from musubi.retrieve.recent import _provenance_score_for

    assert _provenance_score_for(plane="episodic", state=None) is None, (
        "row-factory seam: provenance_score must be None when state is None (no fabrication)"
    )
    # And the table value for a known pair is preserved exactly (not 0.1).
    assert _provenance_score_for(plane="episodic", state="matured") == 0.5, (
        "row-factory seam: provenance_score must equal _PROVENANCE[(episodic, matured)] = 0.5"
    )
    # And an absent pair returns None (not a fabrication default).
    assert _provenance_score_for(plane="curated", state="provisional") is None, (
        "row-factory seam: provenance_score must be None for (curated, provisional) not in _PROVENANCE"
    )


# =====================================================================
# SECTION 6.3 — Source-truth vs internal-default (1 strict red)
# =====================================================================


def test_wire_importance_audits_internal_default(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """Raw `importance` is null for missing source; `score_components.importance` is 0.5 from internal Hit default.

    Seeded via RAW qdrant.upsert with importance ABSENT (not the model default
    of 5) so the wire's `importance: null` reflects the actual source absence.
    """
    ns = "eric/claude-code/episodic"
    marker = _marker()
    object_id = str(generate_ksuid())

    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=object_id,
        payload={
            "namespace": ns,
            "object_id": object_id,
            "state": "matured",  # DATA-001 P2: state filtered post-hydration; seed it visible
            # importance is ABSENT here. Internal Hit default is 5 (0.5 normalized).
        },
        marker_in_content=marker,
    )

    # Prove importance is absent at the store.
    raw_point = qdrant.retrieve(
        collection_name=collection_for_plane("episodic"),
        ids=[episodic_point_id(object_id)],
        with_payload=True,
    )
    assert raw_point and raw_point[0].payload is not None
    assert "importance" not in raw_point[0].payload, (
        f"importance should be absent in source; got {raw_point[0].payload.get('importance')!r}"
    )

    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": marker, "mode": "deep", "limit": 10},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    row = _find_row_by_object_id(results, object_id)
    assert row is not None, (
        f"seeded row {object_id} not returned; results object_ids: {[r.get('object_id') for r in results]}"
    )
    # Raw wire importance is null (source-backed, not the internal default).
    assert row["importance"] is None, (
        f"raw wire importance must be null when source missing; got {row['importance']!r}"
    )
    # Score_components.importance is 0.5 (the internal default normalized).
    assert row["extra"]["score_components"]["importance"] == 0.5, (
        f"score_components.importance must be 0.5 from internal Hit default; "
        f"got {row['extra']['score_components']['importance']!r}"
    )


# =====================================================================
# SECTION 6.4 — Runtime OpenAPI schema (2 strict reds)
# =====================================================================


def test_runtime_openapi_ranked_response_schema_required_with_five_components(
    client: TestClient,
) -> None:
    """GET /v1/openapi.json → RankedRetrieveResponse required has [mode, results, limit]; RankedResultRow required has 7 fields; RankedScoreComponents has 5 properties."""
    r = requests_get("/v1/openapi.json", client)
    doc = r.json()

    schemas = doc.get("components", {}).get("schemas", {})

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

    assert ranked_response is not None, (
        "RankedRetrieveResponse schema missing from runtime openapi.json"
    )
    assert set(ranked_response.get("required", [])) == {"mode", "results", "limit"}, (
        f"RankedRetrieveResponse required must be [mode, results, limit]; got {ranked_response.get('required')}"
    )
    assert ranked_row is not None, "RankedResultRow schema missing from runtime openapi.json"
    # Yua #3: required set is 9 fields (object_id, namespace, plane, score,
    # content, state, importance, score_kind, extra). title is optional.
    # state and importance are required-nullable (present, value may be null)
    # without a Pydantic/OpenAPI default.
    expected_row_required = {
        "object_id",
        "namespace",
        "plane",
        "score",
        "content",
        "state",
        "importance",
        "score_kind",
        "extra",
    }
    assert set(ranked_row.get("required", [])) == expected_row_required, (
        f"RankedResultRow required must be {expected_row_required}; got {set(ranked_row.get('required', []))}"
    )
    # Yua #3: required-nullable without defaults. state, importance are
    # required keys in the schema (present on every row), but the value
    # may be null. A field with `default=None` is OPTIONAL, not
    # required-nullable. Verify those fields do not declare a default.
    for prop_name in ("state", "importance"):
        prop = ranked_row.get("properties", {}).get(prop_name, {})
        assert "default" not in prop, (
            f"RankedResultRow.{prop_name} must be required-nullable WITHOUT default "
            f"(present on every row, value may be null); got default={prop.get('default')!r}"
        )
    assert ranked_components is not None, (
        "RankedScoreComponents schema missing from runtime openapi.json"
    )
    assert set(ranked_components.get("properties", {}).keys()) == {
        "relevance",
        "recency",
        "importance",
        "provenance",
        "reinforcement",
    }, (
        f"RankedScoreComponents must have exactly 5 properties; "
        f"got {set(ranked_components.get('properties', {}).keys())}"
    )
    # Yua #3: required-nullable without defaults. state and importance are
    # required keys in the schema (present on every row), but the value
    # may be null. A field with `default=None` is OPTIONAL, not
    # required-nullable. Verify no property declares a default.
    for prop_name, prop in ranked_components.get("properties", {}).items():
        assert "default" not in prop, (
            f"RankedScoreComponents property {prop_name!r} must not declare a default "
            f"(required-nullable without default); got {prop.get('default')!r}"
        )


def test_runtime_openapi_recent_response_schema_required_with_empty_components(
    client: TestClient,
) -> None:
    """GET /v1/openapi.json → RecentRetrieveResponse required has [mode, results, limit]; RecentResultRow required has 8 fields; RecentScoreComponents is exact {} (additionalProperties:false)."""
    r = requests_get("/v1/openapi.json", client)
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

    assert recent_response is not None, (
        "RecentRetrieveResponse schema missing from runtime openapi.json"
    )
    assert set(recent_response.get("required", [])) == {"mode", "results", "limit"}, (
        f"RecentRetrieveResponse required must be [mode, results, limit]; got {recent_response.get('required')}"
    )
    assert recent_row is not None, "RecentResultRow schema missing from runtime openapi.json"
    # Yua #3: required set is 10 fields (ranked 9 + provenance_score). title
    # is optional. state, importance, provenance_score are required-nullable
    # (present, value may be null) without a Pydantic/OpenAPI default.
    expected_row_required = {
        "object_id",
        "namespace",
        "plane",
        "score",
        "content",
        "state",
        "importance",
        "score_kind",
        "provenance_score",
        "extra",
    }
    assert set(recent_row.get("required", [])) == expected_row_required, (
        f"RecentResultRow required must be {expected_row_required}; got {set(recent_row.get('required', []))}"
    )
    # Yua #3: required-nullable without defaults.
    for prop_name in ("state", "importance", "provenance_score"):
        prop = recent_row.get("properties", {}).get(prop_name, {})
        assert "default" not in prop, (
            f"RecentResultRow.{prop_name} must be required-nullable WITHOUT default "
            f"(present on every row, value may be null); got default={prop.get('default')!r}"
        )
    assert recent_components is not None, (
        "RecentScoreComponents schema missing from runtime openapi.json"
    )
    # RecentScoreComponents is exact {} (additionalProperties: false; no declared properties).
    assert recent_components.get("additionalProperties") is False, (
        f"RecentScoreComponents must have additionalProperties:false; "
        f"got {recent_components.get('additionalProperties')!r}"
    )
    assert not recent_components.get("properties"), (
        f"RecentScoreComponents must have NO declared properties (exact {{}}); "
        f"got {recent_components.get('properties')!r}"
    )


# =====================================================================
# SECTION 6.4b — Discriminator + score-component exactness + recent typed-empty guard
#                 (Yua 2026-07-13 11:57:59 #4, #5, #6)
# =====================================================================


def test_runtime_openapi_retrieve_response_is_oneof_mode_discriminated(
    client: TestClient,
) -> None:
    """POST /v1/retrieve 200 response is a `oneOf` of RankedRetrieveResponse and
    RecentRetrieveResponse with a top-level `mode` discriminator and correct
    mapping/const enums.

    Per Yua 2026-07-13 11:57:59 #4, component-name existence is not enough;
    the discriminator must be present in the openapi schema.
    """
    r = requests_get("/v1/openapi.json", client)
    doc = r.json()

    paths = doc.get("paths", {})
    retrieve_path = paths.get("/v1/retrieve")
    assert retrieve_path is not None, "/v1/retrieve path missing from openapi.json"
    post_op = retrieve_path.get("post")
    assert post_op is not None, "POST /v1/retrieve operation missing"
    response_200 = post_op.get("responses", {}).get("200")
    assert response_200 is not None, "POST /v1/retrieve 200 response missing"
    content = response_200.get("content", {})
    json_content = content.get("application/json")
    assert json_content is not None, "POST /v1/retrieve 200 application/json content missing"
    schema_ref = json_content.get("schema", {})
    one_of = schema_ref.get("oneOf")
    assert one_of is not None, (
        f"POST /v1/retrieve 200 schema must be a oneOf with a mode discriminator; got {schema_ref!r}"
    )
    assert len(one_of) == 2, (
        f"POST /v1/retrieve 200 oneOf must have exactly 2 variants (ranked + recent); got {len(one_of)}"
    )
    discriminator = schema_ref.get("discriminator")
    assert discriminator is not None, "POST /v1/retrieve 200 schema must declare a discriminator"
    assert discriminator.get("propertyName") == "mode", (
        f"discriminator must be on top-level `mode`; got {discriminator.get('propertyName')!r}"
    )
    mapping = discriminator.get("mapping") or {}
    assert mapping.get("fast") == "#/components/schemas/RankedRetrieveResponse", (
        f"discriminator mapping[fast] must point to RankedRetrieveResponse; got {mapping.get('fast')!r}"
    )
    assert mapping.get("deep") == "#/components/schemas/RankedRetrieveResponse", (
        f"discriminator mapping[deep] must point to RankedRetrieveResponse; got {mapping.get('deep')!r}"
    )
    assert mapping.get("blended") == "#/components/schemas/RankedRetrieveResponse", (
        f"discriminator mapping[blended] must point to RankedRetrieveResponse; got {mapping.get('blended')!r}"
    )
    assert mapping.get("recent") == "#/components/schemas/RecentRetrieveResponse", (
        f"discriminator mapping[recent] must point to RecentRetrieveResponse; got {mapping.get('recent')!r}"
    )


def test_retrieve_ranked_extra_score_components_exactly_five_and_values_in_unit_interval(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """`extra.score_components` has EXACTLY the 5 public contributors and every value is numeric in [0,1].

    Per Yua 2026-07-13 11:57:59 #5, the previous test checked only
    `expected - keys` empty; a fabricated key would pass. The
    implementation must expose exactly the 5 public contributors and
    clamp every value to [0,1].
    """
    ns = "eric/claude-code/episodic"
    marker = _marker()
    object_id = str(generate_ksuid())

    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=object_id,
        payload={
            "namespace": ns,
            "object_id": object_id,
            "state": "matured",
            "importance": 7,
        },
        marker_in_content=marker,
    )

    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": marker, "mode": "fast", "limit": 10},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    row = _find_row_by_object_id(results, object_id)
    assert row is not None, (
        f"seeded row {object_id} not returned; results object_ids: {[r.get('object_id') for r in results]}"
    )
    components = row.get("extra", {}).get("score_components", {})
    expected = {"relevance", "recency", "importance", "provenance", "reinforcement"}
    assert set(components.keys()) == expected, (
        f"`extra.score_components` keys must be EXACTLY {expected}; got {sorted(components.keys())}"
    )
    for k, v in components.items():
        assert isinstance(v, (int, float)) and not isinstance(v, bool), (
            f"score_components[{k!r}] must be numeric; got {v!r}"
        )
        assert 0.0 <= float(v) <= 1.0, f"score_components[{k!r}] must be in [0,1]; got {v!r}"


def test_recent_score_components_typed_empty_runtime_rejects_nonempty() -> None:
    """The runtime `RecentScoreComponents` Pydantic model rejects a non-empty dict at validation.

    Per Yua 2026-07-13 11:57:59 #6, OpenAPI `additionalProperties:false`
    alone is not execution proof. The runtime Pydantic model must
    reject a non-empty input at validation time.
    """
    from pydantic import ValidationError

    # The implementation will land `musubi.api.responses.RecentScoreComponents`.
    from musubi.api.responses import RecentScoreComponents

    obj = RecentScoreComponents()
    assert obj.model_dump() == {}, (
        f"RecentScoreComponents() must be the exact empty {{}}; got {obj.model_dump()!r}"
    )
    with pytest.raises(ValidationError) as excinfo:
        RecentScoreComponents.model_validate({"relevance": 0.0})
    err_str = str(excinfo.value).lower()
    assert "extra" in err_str or "additional" in err_str or "forbid" in err_str, (
        f"RecentScoreComponents must reject non-empty input as a forbid-extra violation; got {excinfo.value!r}"
    )


# =====================================================================
# SECTION 6.5 — Regression guards (3; #8 reclassified from §6.1)
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
    r = requests_get("/v1/openapi.json", client)
    doc = r.json()
    paths = doc.get("paths", {})
    assert "/v1/retrieve/stream" in paths, (
        f"streaming endpoint missing from runtime openapi; available paths: {sorted(paths.keys())}"
    )


def test_extra_score_components_path_preserved_for_all_modes(
    client: TestClient, auth: dict[str, str], qdrant: QdrantClient
) -> None:
    """`extra.score_components` is present at the same path for both ranked and recent.

    REGRESSION GUARD: this path is preserved (v1 compat); ranked expands
    3→5 keys; recent is a 3-key dict (currently fabricated but at the same
    path). Do NOT migrate `score_components` to top-level in v1.
    """
    ns = "eric/claude-code/episodic"
    marker = _marker()
    object_id = str(generate_ksuid())

    _raw_upsert(
        qdrant,
        collection=collection_for_plane("episodic"),
        object_id=object_id,
        payload={"namespace": ns, "object_id": object_id, "state": "matured"},  # P2: seed visible
        marker_in_content=marker,
    )

    # Ranked mode: extra.score_components path must be present.
    r_ranked = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "query_text": marker, "mode": "fast", "limit": 10},
    )
    assert r_ranked.status_code == 200
    r_ranked_results = r_ranked.json()["results"]
    row_ranked = _find_row_by_object_id(r_ranked_results, object_id)
    assert row_ranked is not None, (
        f"seeded row {object_id} not in ranked results; object_ids: {[r.get('object_id') for r in r_ranked_results]}"
    )
    assert "extra" in row_ranked, (
        f"ranked row missing `extra`; current shape: {sorted(row_ranked.keys())}"
    )
    assert "score_components" in row_ranked["extra"], (
        f"ranked row missing `extra.score_components`; extra: {list(row_ranked['extra'].keys())}"
    )

    # Recent mode: extra.score_components path must be present.
    r_recent = client.post(
        "/v1/retrieve",
        headers=auth,
        json={"namespace": ns, "mode": "recent", "limit": 10},
    )
    assert r_recent.status_code == 200
    r_recent_results = r_recent.json()["results"]
    row_recent = _find_row_by_object_id(r_recent_results, object_id)
    assert row_recent is not None, (
        f"seeded row {object_id} not in recent results; object_ids: {[r.get('object_id') for r in r_recent_results]}"
    )
    assert "extra" in row_recent, (
        f"recent row missing `extra`; current shape: {sorted(row_recent.keys())}"
    )
    assert "score_components" in row_recent["extra"], (
        f"recent row missing `extra.score_components`; extra: {list(row_recent['extra'].keys())}"
    )


# =====================================================================
# SECTION 7 — Implementation slice additions (per Yua 2026-07-13 12:24:42)
#                - runtime-vs-snapshot parity
#                - Hermes JSON pass-through conformance
# =====================================================================


# =====================================================================
# Section: openapi parity (per Yua 12:45:46 #4 + 12:59:30 BLOCKER A)
# Shared normalizer + full-document equality + red-proof + one_field_drift mutation
# =====================================================================


def _normalize_openapi_for_parity(doc: Any) -> Any:
    """Strip ONLY the EXPLICIT PERMIT SET (info.title + info.version).

    Per Yua 2026-07-13 12:59:30 BLOCKER A: the normalizer must
    change/remove ONLY info.title + info.version while preserving
    every OTHER info field. ALL non-permitted fields are compared
    FULLY. A one-field drift anywhere else is a parity failure.

    The runtime FastAPI's `info` carries a generated title/version;
    the committed openapi.yaml may pin a release version. Both are
    EXPLICITLY permitted to differ. All other info fields (e.g.
    `description`, `contact`, `license`, `termsOfService`,
    `x-logo`) must agree across runtime and snapshot.
    """
    import copy as _copy

    out = _copy.deepcopy(doc)
    info = out.get("info")
    if isinstance(info, dict):
        # The EXPLICIT PERMIT SET -- strip only these two keys.
        info.pop("title", None)
        info.pop("version", None)
    return out


def test_runtime_vs_snapshot_openapi_schema_parity(client: TestClient) -> None:
    """The runtime /v1/openapi.json must FULLY EQUAL the committed openapi.yaml
    modulo the EXPLICIT PERMIT SET (info.title + info.version).

    Per Yua 2026-07-13 12:59:30 BLOCKER A: the previous test
    only checked SELECTED PRESENCE (a few named schemas + the
    /v1/retrieve 200 schema); it did NOT assert full-document
    equality. The OLD selective check is replaced with a strict
    full-document equality using the shared normalizer above.
    """
    import yaml as _yaml

    r = requests_get("/v1/openapi.json", client)
    assert r.status_code == 200
    runtime = r.json()
    snapshot_path = Path(__file__).resolve().parents[2] / "openapi.yaml"
    assert snapshot_path.exists(), f"committed openapi.yaml not found at {snapshot_path}"
    with snapshot_path.open() as f:
        snapshot = _yaml.safe_load(f)

    # Apply the SHARED NORMALIZER to BOTH. Strips ONLY info.title
    # and info.version. Preserves every other info field.
    norm_runtime = _normalize_openapi_for_parity(runtime)
    norm_snapshot = _normalize_openapi_for_parity(snapshot)

    # FULL-DOCUMENT EQUALITY (modulo the EXPLICIT PERMIT SET).
    # A one-field drift in any non-permitted field fails.
    if norm_runtime != norm_snapshot:
        import difflib as _difflib
        import json as _json

        rt = _json.dumps(norm_runtime, indent=2, sort_keys=True)
        sn = _json.dumps(norm_snapshot, indent=2, sort_keys=True)
        diff = list(
            _difflib.unified_diff(
                sn.splitlines(),
                rt.splitlines(),
                lineterm="",
                n=2,
            )
        )[:30]
        pytest.fail(
            "runtime /v1/openapi.json != committed openapi.yaml "
            "(normalized via _normalize_openapi_for_parity; strips "
            "ONLY info.title and info.version; preserves every "
            "other info field). First drift:\n" + chr(10).join(diff)
        )


def test_runtime_vs_snapshot_parity_one_field_drift_mutation_proof() -> None:
    """A one-field drift in a non-permitted field FAILS the parity check.

    Per Yua 2026-07-13 12:59:30 BLOCKER A: the mutation proof must
    "modify one arbitrary non-permitted field on a deep copy and
    prove comparator fails." The mutation takes a deep copy of
    the snapshot, mutates ONE field in a non-permitted location
    (e.g. ``components.schemas.RankedScoreComponents.description``),
    and asserts the new strict parity check rejects it.
    """
    import copy as _copy

    import yaml as _yaml

    snapshot_path = Path(__file__).resolve().parents[2] / "openapi.yaml"
    assert snapshot_path.exists(), f"committed openapi.yaml not found at {snapshot_path}"
    with snapshot_path.open() as f:
        snapshot = _yaml.safe_load(f)

    # Deep copy + mutate ONE non-permitted field.
    mutated = _copy.deepcopy(snapshot)
    mutated.setdefault("components", {}).setdefault("schemas", {}).setdefault(
        "RankedScoreComponents", {}
    )["description"] = (
        "MUTATED one-field drift -- this should be caught by the strict "
        "parity check (the OLD selective check would have missed it)."
    )
    assert snapshot != mutated, "test setup failed: mutation did not change snapshot"

    # Apply the shared normalizer to BOTH and assert inequality.
    norm_snapshot = _normalize_openapi_for_parity(snapshot)
    norm_mutated = _normalize_openapi_for_parity(mutated)
    assert norm_snapshot != norm_mutated, (
        "strict parity check did not detect a one-field drift in "
        "components.schemas.RankedScoreComponents.description; the test "
        "is not sensitive to one-field drift"
    )

    # RED-PROOF: simulate the OLD selective checker (which only
    # checked selected presence, NOT full equality) and show it
    # would have PASSED on the mutated snapshot.
    old_checker_schemas = ["RankedRetrieveResponse", "RecentRetrieveResponse"]
    for name in old_checker_schemas:
        assert name in mutated.get("components", {}).get("schemas", {}), (
            f"old checker: schema {name!r} must be present (still passes under mutation)"
        )
    retrieve_path = mutated.get("paths", {}).get("/v1/retrieve", {})
    schema = (
        retrieve_path.get("post", {})
        .get("responses", {})
        .get("200", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    one_of = schema.get("oneOf") or schema.get("anyOf")
    assert one_of is not None and len(one_of) == 2, (
        "old checker: oneOf with 2 variants must be present (still passes under mutation)"
    )
    # The OLD selective checker would have PASSED on the mutated
    # snapshot (the named schemas are present; the oneOf has 2
    # variants). The NEW strict check FAILS. The new test is more
    # sensitive.


def test_runtime_vs_snapshot_parity_normalizer_preserves_other_info_fields() -> None:
    """The normalizer strips ONLY info.title and info.version; preserves every other info field.

    Per Yua 2026-07-13 12:59:30 BLOCKER A: the normalizer must
    "change/remove ONLY info.title + info.version while preserving
    every other info field." This test asserts the normalizer
    preserves ``info.description``, ``info.contact``, ``info.license``,
    and any other non-permitted info field it encounters.
    """

    doc: Any = {
        "info": {
            "title": "Musubi Core API",
            "version": "0.1.0",
            "description": "The canonical HTTP surface over Musubi Core.",
            "contact": {"name": "Tama", "email": "tama@harem-ops"},
            "license": {"name": "Apache-2.0"},
        },
        "openapi": "3.1.0",
    }
    normalized = _normalize_openapi_for_parity(doc)
    info = normalized["info"]
    # The PERMIT SET is stripped.
    assert "title" not in info, "normalizer must strip info.title (permit set)"
    assert "version" not in info, "normalizer must strip info.version (permit set)"
    # All OTHER info fields are preserved EXACTLY.
    assert info["description"] == "The canonical HTTP surface over Musubi Core."
    assert info["contact"] == {"name": "Tama", "email": "tama@harem-ops"}
    assert info["license"] == {"name": "Apache-2.0"}


# Section: Hermes adapter wire-readiness (renamed from hernes_..., per Yua #5)


def test_musubi_wire_readiness_passthrough_shape() -> None:
    """The Musubi wire shape preserves the fields the Hermes adapter must pass through.

    Per Yua 2026-07-13 12:45:46 #5: the actual Hermes user plugin
    (at /Users/ericmey/Vaults/fleet-tools/hermes-plugins/musubi/__init__.py)
    has its OWN transform keep-list. This test is the **Musubi
    wire-readiness** check — it loads the runtime response models
    and asserts the shape is exactly what the Hermes plugin must
    surface to its JSON callers. The actual plugin transform is
    tested separately in /Users/ericmey/Vaults/fleet-tools (additive
    branch on main; see commit history).

    Per Yua 2026-07-13 12:24:42 ("add the already-required Hermes
    JSON pass-through conformance"), the wire must surface, for every
    ranked row, the fields the Hermes adapter must pass through
    without fabrication:

      - top-level `state` (LifecycleState enum, 7 values, nullable for missing legacy)
      - top-level `importance` (int 1..10, nullable for missing legacy)
      - top-level `score_kind: "ranked_combined"`
      - top-level `object_id` (the stored KSUID; the LOGICAL API id, NOT
        the physical Qdrant point id)
      - `extra.score_components` (5 keys: relevance, recency, importance,
        provenance, reinforcement)

    This test loads the runtime response models and asserts the
    shape is exactly what the Hermes plugin must surface to its JSON
    callers. The test is the conformance gate; the Hermes adapter
    work is a separate slice.
    """
    from musubi.api.responses import (
        RankedExtra,
        RankedResultRow,
        RankedRetrieveResponse,
        RankedScoreComponents,
        RecentExtra,
        RecentResultRow,
        RecentRetrieveResponse,
        RecentScoreComponents,
    )

    # Ranked row must surface the 5-key score_components and top-level
    # state/importance/score_kind.
    ranked_row = RankedResultRow(
        object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
        namespace="eric/claude-code/episodic",
        plane="episodic",
        score=0.875,
        content="snippet",
        state="matured",
        importance=7,
        score_kind="ranked_combined",
        extra=RankedExtra(
            score_components=RankedScoreComponents(
                relevance=1.0,
                recency=1.0,
                importance=0.7,
                provenance=0.5,
                reinforcement=0.0,
            ),
        ),
    )
    dumped = ranked_row.model_dump()
    # Top-level keys the Hermes adapter must pass through.
    assert "object_id" in dumped
    assert dumped["object_id"] == "3GSGzQauqzXNPstBMJw3hcIV0yd"
    assert "state" in dumped and dumped["state"] == "matured"
    assert "importance" in dumped and dumped["importance"] == 7
    assert "score_kind" in dumped and dumped["score_kind"] == "ranked_combined"
    # extra.score_components has the 5 keys.
    assert set(dumped["extra"]["score_components"].keys()) == {
        "relevance",
        "recency",
        "importance",
        "provenance",
        "reinforcement",
    }
    # Recent row: state/importance top-level, score_kind=created_epoch,
    # provenance_score nullable, score_components exact {}.
    recent_row = RecentResultRow(
        object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
        namespace="eric/claude-code/episodic",
        plane="episodic",
        score=1783957804.0,
        content="snippet",
        state="matured",
        importance=7,
        score_kind="created_epoch",
        provenance_score=0.5,
        extra=RecentExtra(score_components=RecentScoreComponents()),
    )
    dumped_recent = recent_row.model_dump()
    assert dumped_recent["score_kind"] == "created_epoch"
    assert dumped_recent["provenance_score"] == 0.5
    assert dumped_recent["extra"]["score_components"] == {}
    # Top-level response variants carry `mode` as the discriminator.
    ranked_resp = RankedRetrieveResponse(
        mode="fast",
        results=[],
        limit=5,
    )
    assert ranked_resp.model_dump()["mode"] == "fast"
    recent_resp = RecentRetrieveResponse(
        mode="recent",
        results=[],
        limit=5,
    )
    assert recent_resp.model_dump()["mode"] == "recent"
    # RecentScoreComponents is exactly {}.
    assert RecentScoreComponents().model_dump() == {}


# =====================================================================
# SECTION 8 — Yua 2026-07-13 12:45:46 WITHHOLD corrections
#                #1 response variants; #2 strict numeric; #3 required score_components;
#                #4 parity equality
# =====================================================================


def test_ranked_response_rejects_recent_row_mutation() -> None:
    """RankedRetrieveResponse.results: list[RankedResultRow] REJECTS a RecentResultRow.

    Per Yua 2026-07-13 12:45:46 #1: "Contract requires ranked
    results:list[RankedResultRow], recent results:list[RecentResultRow].
    Add exact ref tests + wrong-row rejection." A recent row
    smuggled into a ranked response must FAIL the Pydantic validation.
    """
    from pydantic import ValidationError

    from musubi.api.responses import RankedRetrieveResponse, RecentResultRow

    recent_row = RecentResultRow(
        object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
        namespace="eric/claude-code/episodic",
        plane="episodic",
        score=1783957804.0,
        content="snippet",
        state="matured",
        importance=7,
        score_kind="created_epoch",
        provenance_score=0.5,
        extra=RecentExtra(score_components=RecentScoreComponents()),
    )
    # The recent row's score_kind is "created_epoch", NOT
    # "ranked_combined". A RankedRetrieveResponse that accepts this
    # row would violate the contract.
    with pytest.raises(ValidationError):
        RankedRetrieveResponse(
            mode="fast",
            results=[recent_row],  # type: ignore[list-item]
            limit=5,
        )


def test_recent_response_rejects_ranked_row_mutation() -> None:
    """RecentRetrieveResponse.results: list[RecentResultRow] REJECTS a RankedResultRow.

    Per Yua 2026-07-13 12:45:46 #1. A ranked row smuggled into a
    recent response must FAIL the Pydantic validation.
    """
    from pydantic import ValidationError

    from musubi.api.responses import RankedResultRow, RecentRetrieveResponse

    ranked_row = RankedResultRow(
        object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
        namespace="eric/claude-code/episodic",
        plane="episodic",
        score=0.875,
        content="snippet",
        state="matured",
        importance=7,
        score_kind="ranked_combined",
        extra=RankedExtra(
            score_components=RankedScoreComponents(
                relevance=1.0,
                recency=1.0,
                importance=0.7,
                provenance=0.5,
                reinforcement=0.0,
            ),
        ),
    )
    # The ranked row's score_kind is "ranked_combined", NOT
    # "created_epoch". A RecentRetrieveResponse that accepts this row
    # would violate the contract.
    with pytest.raises(ValidationError):
        RecentRetrieveResponse(
            mode="recent",
            results=[ranked_row],  # type: ignore[list-item]
            limit=5,
        )


def test_ranked_importance_rejects_str_coercion_mutation() -> None:
    """RankedResultRow.importance REJECTS str "7" (coerced to 7) and bool True.

    Per Yua 2026-07-13 12:45:46 #2: "importance="7" coerces to 7;
    score component "0.1" coerces to float. Use strict numeric
    validation (accept real int/float as intended, reject
    str/bool/coercion) and mutation proofs." StrictInt rejects
    str/bool; real int passes.
    """
    from pydantic import ValidationError

    # str "7" is REJECTED (Pydantic would coerce by default; StrictInt
    # rejects it per Yua #2).
    with pytest.raises(ValidationError):
        RankedResultRow(
            object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
            namespace="eric/claude-code/episodic",
            plane="episodic",
            score=0.875,
            content="snippet",
            state="matured",
            importance="7",  # type: ignore[arg-type]
            score_kind="ranked_combined",
            extra=RankedExtra(
                score_components=RankedScoreComponents(
                    relevance=1.0,
                    recency=1.0,
                    importance=0.7,
                    provenance=0.5,
                    reinforcement=0.0,
                ),
            ),
        )
    # bool True is REJECTED (True is technically an int in Python;
    # StrictInt rejects it).
    with pytest.raises(ValidationError):
        RankedResultRow(
            object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
            namespace="eric/claude-code/episodic",
            plane="episodic",
            score=0.875,
            content="snippet",
            state="matured",
            importance=True,
            score_kind="ranked_combined",
            extra=RankedExtra(
                score_components=RankedScoreComponents(
                    relevance=1.0,
                    recency=1.0,
                    importance=0.7,
                    provenance=0.5,
                    reinforcement=0.0,
                ),
            ),
        )
    # Reference: real int 7 passes.
    ref = RankedResultRow(
        object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
        namespace="eric/claude-code/episodic",
        plane="episodic",
        score=0.875,
        content="snippet",
        state="matured",
        importance=7,
        score_kind="ranked_combined",
        extra=RankedExtra(
            score_components=RankedScoreComponents(
                relevance=1.0,
                recency=1.0,
                importance=0.7,
                provenance=0.5,
                reinforcement=0.0,
            ),
        ),
    )
    assert ref.importance == 7


def test_ranked_score_component_rejects_str_coercion_mutation() -> None:
    """RankedScoreComponents values REJECT str "0.1" and bool True; accept real float.

    Per Yua 2026-07-13 12:45:46 #2: "score component "0.1" coerces
    to float. Use strict numeric validation (accept real int/float
    as intended, reject str/bool/coercion)."
    """
    from pydantic import ValidationError

    # str "0.1" is REJECTED.
    with pytest.raises(ValidationError):
        RankedScoreComponents(
            relevance="0.1",  # type: ignore[arg-type]
            recency=1.0,
            importance=0.7,
            provenance=0.5,
            reinforcement=0.0,
        )
    # bool True is REJECTED.
    with pytest.raises(ValidationError):
        RankedScoreComponents(
            relevance=True,
            recency=1.0,
            importance=0.7,
            provenance=0.5,
            reinforcement=0.0,
        )
    # Reference: real float 0.1 passes.
    ref = RankedScoreComponents(
        relevance=0.1,
        recency=1.0,
        importance=0.7,
        provenance=0.5,
        reinforcement=0.0,
    )
    assert ref.relevance == 0.1


def test_recent_provenance_score_bounded_mutation() -> None:
    """RecentResultRow.provenance_score REJECTS values outside [0.0, 1.0] and rejects str/bool.

    Per Yua 2026-07-13 12:45:46 #2: "bound recent provenance_score
    0..1 if non-null." Also rejects str (StrictFloat).
    """
    from pydantic import ValidationError

    # Out of range (negative).
    with pytest.raises(ValidationError):
        RecentResultRow(
            object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
            namespace="eric/claude-code/episodic",
            plane="episodic",
            score=1783957804.0,
            content="snippet",
            state="matured",
            importance=7,
            score_kind="created_epoch",
            provenance_score=-0.1,  # out of [0, 1]
            extra=RecentExtra(score_components=RecentScoreComponents()),
        )
    # Out of range (>1.0).
    with pytest.raises(ValidationError):
        RecentResultRow(
            object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
            namespace="eric/claude-code/episodic",
            plane="episodic",
            score=1783957804.0,
            content="snippet",
            state="matured",
            importance=7,
            score_kind="created_epoch",
            provenance_score=1.5,  # out of [0, 1]
            extra=RecentExtra(score_components=RecentScoreComponents()),
        )
    # str is REJECTED (StrictFloat).
    with pytest.raises(ValidationError):
        RecentResultRow(
            object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
            namespace="eric/claude-code/episodic",
            plane="episodic",
            score=1783957804.0,
            content="snippet",
            state="matured",
            importance=7,
            score_kind="created_epoch",
            provenance_score="0.5",  # type: ignore[arg-type]
            extra=RecentExtra(score_components=RecentScoreComponents()),
        )
    # Reference: real float 0.5 passes.
    ref = RecentResultRow(
        object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
        namespace="eric/claude-code/episodic",
        plane="episodic",
        score=1783957804.0,
        content="snippet",
        state="matured",
        importance=7,
        score_kind="created_epoch",
        provenance_score=0.5,
        extra=RecentExtra(score_components=RecentScoreComponents()),
    )
    assert ref.provenance_score == 0.5


def test_recent_extra_score_components_required_mutation() -> None:
    """RecentExtra.score_components is REQUIRED; missing input REJECTS.

    Per Yua 2026-07-13 12:45:46 #3: "RecentExtra.score_components
    has a default_factory, so missing input fabricates `{}` and
    OpenAPI does not require it. Make it required; assert missing
    and nonempty both reject, and required set includes
    score_components."

    RecentExtra REQUIRES score_components (no default_factory).
    Missing `score_components` REJECTS; non-empty input REJECTS (the
    typed `RecentScoreComponents` is `extra=forbid`); `{}` is the
    ONLY valid value.
    """
    from pydantic import ValidationError

    # Missing: REJECTS.
    with pytest.raises(ValidationError):
        RecentExtra()  # type: ignore[call-arg]

    # Empty `{}`: ACCEPTS.
    extra_empty = RecentExtra(score_components=RecentScoreComponents())
    assert extra_empty.model_dump() == {"score_components": {}, "lineage": {}}

    # Non-empty input: REJECTS (RecentScoreComponents is extra=forbid).
    with pytest.raises(ValidationError):
        RecentExtra(score_components=RecentScoreComponents.model_validate({"relevance": 0.0}))

    # OpenAPI required set: assert the generated schema lists
    # score_components in `required`. Tested in the openapi tests
    # but assert here too for directness.
    schema = RecentExtra.model_json_schema()
    assert "score_components" in schema.get("required", []), (
        f"RecentExtra required must include score_components; got {schema.get('required')}"
    )
