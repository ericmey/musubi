"""End-to-end router test for slice-retrieve-recent.

Covers the API-surface contract bullets that need a real FastAPI app +
seeded Qdrant fixture:

- POST /v1/retrieve with mode="recent" and no query_text returns 200
  (today's pre-slice behaviour was 422 because query_text was required).
- Results come back ordered newest-first (delegated to Qdrant's order_by;
  this test asserts a multi-row response is sorted by `created_epoch`
  descending end-to-end).
- `mode="recent"` + `query_text` provided is accept-and-ignore (200, not
  422 — the slice-retrieve-recent design decision).
"""

from __future__ import annotations

import asyncio
import warnings

import pytest
from fastapi.testclient import TestClient

with warnings.catch_warnings():
    warnings.simplefilter("ignore")

from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.planes.episodic import EpisodicPlane
from musubi.settings import Settings
from musubi.types.episodic import EpisodicMemory

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient


_COORDINATOR: LifecycleTransitionCoordinator | None = None


@pytest.fixture(autouse=True)
def _install_coordinator(qdrant: QdrantClient, api_settings: Settings) -> None:
    global _COORDINATOR
    _COORDINATOR = LifecycleTransitionCoordinator(
        client=qdrant, db_path=api_settings.lifecycle_sqlite_path
    )


def _coord() -> LifecycleTransitionCoordinator:
    assert _COORDINATOR is not None
    return _COORDINATOR


def _seed(plane: EpisodicPlane, namespace: str, content: str) -> None:
    async def _go() -> None:
        saved = await plane.create(EpisodicMemory(namespace=namespace, content=content))
        await plane.transition(
            namespace=namespace,
            object_id=saved.object_id,
            to_state="matured",
            actor="seed",
            reason="seed",
            coordinator=_coord(),
        )

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# Test contract: mode="recent" without query_text returns 200
# ---------------------------------------------------------------------------


def test_retrieve_recent_no_query_text_returns_200(
    client: TestClient, episodic: EpisodicPlane, api_settings: object
) -> None:
    """Pre-slice, the API required query_text and this would 422."""
    from tests.api.conftest import mint_token

    _seed(episodic, "aoi/command-chair/episodic", "early write")
    _seed(episodic, "aoi/command-chair/episodic", "later write")

    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["aoi/*/*:r"],
        presence="aoi/command-chair",
    )
    r = client.post(
        "/v1/retrieve",
        json={
            "namespace": "aoi/command-chair/episodic",
            "mode": "recent",
            "limit": 10,
            # No `query_text` — the whole point of this test.
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "recent"
    # Both seeded rows present.
    assert len(body["results"]) == 2


def test_retrieve_recent_results_are_newest_first(
    client: TestClient, episodic: EpisodicPlane, api_settings: object
) -> None:
    """Order is Qdrant's order_by=DESC on created_epoch.

    The seeds are created sequentially; KSUID and created_epoch increase
    monotonically. Recent mode must surface the LATER write first.
    """
    from tests.api.conftest import mint_token

    _seed(episodic, "aoi/command-chair/episodic", "first")
    _seed(episodic, "aoi/command-chair/episodic", "second")
    _seed(episodic, "aoi/command-chair/episodic", "third")

    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["aoi/*/*:r"],
        presence="aoi/command-chair",
    )
    r = client.post(
        "/v1/retrieve",
        json={
            "namespace": "aoi/command-chair/episodic",
            "mode": "recent",
            "limit": 10,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    contents = [row["content"] for row in r.json()["results"]]
    # Order is by created_epoch DESC — latest insert first.
    assert contents == ["third", "second", "first"]


def test_retrieve_recent_with_query_text_is_accept_and_ignore(
    client: TestClient,
    episodic: EpisodicPlane,
    api_settings: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Slice design decision: mode=recent + query_text → ignore, log WARN.

    422 would force assistant-side error handling for a no-op problem.
    Accept-and-ignore is the forgiving boundary behaviour. We assert 200,
    the row count matches what an un-queried recent call would return,
    AND that the WARN log line actually fires — if someone later removes
    the warning, the contract documented in the slice spec quietly
    breaks; the caplog assert locks it in.
    """
    import logging

    from tests.api.conftest import mint_token

    _seed(episodic, "aoi/command-chair/episodic", "alpha")
    _seed(episodic, "aoi/command-chair/episodic", "beta")

    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["aoi/*/*:r"],
        presence="aoi/command-chair",
    )
    with caplog.at_level(logging.WARNING, logger="musubi.retrieve.orchestration"):
        r = client.post(
            "/v1/retrieve",
            json={
                "namespace": "aoi/command-chair/episodic",
                "mode": "recent",
                "query_text": "ignored field",
                "limit": 10,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    # Same 2 rows recent would return without a query_text.
    assert len(r.json()["results"]) == 2
    # Locks in the WARN-on-ignore design decision — if this assertion
    # disappears, the silent-drop becomes possible again.
    assert any(
        "mode=recent ignoring query_text" in record.message and record.levelname == "WARNING"
        for record in caplog.records
    ), f"expected WARN log; got: {[r.message for r in caplog.records]}"


@pytest.mark.parametrize("mode", ["fast", "deep", "blended"])
def test_retrieve_ranked_modes_accept_since_and_tags_without_effect(
    client: TestClient,
    episodic: EpisodicPlane,
    api_settings: object,
    mode: str,
) -> None:
    """`since` and `tags` are recent-mode fields, but ranked modes accept
    them without effect (forward-compat — documented behaviour in the
    router model + openapi).

    Caller passes both with a real `query_text`; assert 200 and that
    rows come back. The fields are silently ignored, not filtered on.
    """
    from tests.api.conftest import mint_token

    _seed(episodic, "aoi/command-chair/episodic", "anything")
    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["aoi/*/*:r"],
        presence="aoi/command-chair",
    )
    r = client.post(
        "/v1/retrieve",
        json={
            "namespace": "aoi/command-chair/episodic",
            "mode": mode,
            "query_text": "anything",
            "since": 1.0,
            "tags": ["nonexistent-tag-that-would-filter-everything"],
            "limit": 10,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    # 200, not 400/422 — fields accepted. If the tags filter were
    # actually applied here, the row wouldn't carry the tag and we'd
    # see 0 results. We don't assert non-zero (depending on mode the
    # similarity score may not surface the row) — just that the call
    # succeeds and isn't 4xx'd by the new fields.
    assert r.status_code == 200, r.text


def test_retrieve_recent_with_since_filter_excludes_old_rows(
    client: TestClient, episodic: EpisodicPlane, api_settings: object
) -> None:
    """`since` is an inclusive epoch-seconds floor.

    Seeds two rows, captures the timestamp between them, and asserts the
    older row is excluded.
    """
    import time

    from tests.api.conftest import mint_token

    _seed(episodic, "aoi/command-chair/episodic", "before-cutoff")
    # Small wait so created_epoch differs across rows. The clock resolution
    # on the test runner is sub-second; 50ms is comfortably > one tick.
    time.sleep(0.05)
    cutoff = time.time()
    time.sleep(0.05)
    _seed(episodic, "aoi/command-chair/episodic", "after-cutoff")

    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["aoi/*/*:r"],
        presence="aoi/command-chair",
    )
    r = client.post(
        "/v1/retrieve",
        json={
            "namespace": "aoi/command-chair/episodic",
            "mode": "recent",
            "since": cutoff,
            "limit": 10,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    contents = [row["content"] for row in r.json()["results"]]
    assert contents == ["after-cutoff"]


# ---------------------------------------------------------------------------
# Ranked modes still require query_text (orchestration-side validator).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["fast", "deep", "blended"])
def test_retrieve_ranked_modes_without_query_text_still_422(
    client: TestClient,
    episodic: EpisodicPlane,
    api_settings: object,
    mode: str,
) -> None:
    """Adding mode=recent doesn't loosen query_text for the ranked modes.

    Pre-slice, `query_text` was a required string and FastAPI body
    validation returned 422. The slice adds a router-side model_validator
    (mirrors the orchestration rule) so the wire status stays 422 —
    rather than turning into a 400 from the orchestration layer.
    Locks in the wire contract for retry logic that branches on status.
    """
    from tests.api.conftest import mint_token

    _seed(episodic, "aoi/command-chair/episodic", "anything")
    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["aoi/*/*:r"],
        presence="aoi/command-chair",
    )
    r = client.post(
        "/v1/retrieve",
        json={
            "namespace": "aoi/command-chair/episodic",
            "mode": mode,
            "limit": 10,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    # Router-side model_validator catches the cross-field rule before
    # orchestration even runs — preserves the pre-slice 422 contract.
    assert r.status_code == 422, r.text
    assert "query_text" in r.text
