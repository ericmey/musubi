"""Tests for ``musubi validate rows``.

The load-bearing test in this file is ``test_total_scan_failure_is_never_clean``.

The first cut of this command caught every scan exception, continued, and then printed
``clean`` and exited 0 whenever no broken rows had been *collected* — including when auth
failed, the network was down, or every collection was misnamed and **nothing was scanned
at all**. A sweep that pronounces the vault healthy because it could not look at the vault
is the exact defect the rest of this PR exists to fix, rebuilt one layer up.

That was the third instance of the same shape in a single day (the vault's stale-check
reported "0 stale" while reading 8 of 164 pages; the frontmatter lint required a field
that existed in zero files so could never pass and was never run; then this). Caught by
Yua in rev2 review.

So these tests do not merely check that the happy path works. They check that the
instrument **cannot lie about having looked.**
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from qdrant_client.http.exceptions import UnexpectedResponse
from typer.testing import CliRunner

from musubi.cli.main import app
from musubi.cli.validate import _PLANE_MODELS, EXIT_INCOMPLETE

runner = CliRunner()

GOOD_EPISODIC: dict[str, Any] = {
    "object_id": "3GJhJLAvYXzIp8Qe8tuPHR9S9th",
    "namespace": "eric/claude-code/episodic",
    "content": "a readable memory",
    "state": "matured",
}


class _Rec:
    def __init__(self, payload: dict[str, Any] | None, point_id: str = "p1") -> None:
        self.payload = payload
        self.id = point_id


class FakeQdrant:
    """Scriptable stand-in. `behaviour` maps collection -> pages | Exception."""

    def __init__(self, behaviour: dict[str, Any]) -> None:
        self._behaviour = behaviour
        self.scroll_calls = 0

    def scroll(self, *, collection_name: str, limit: int, offset: Any, **_: Any) -> Any:
        self.scroll_calls += 1
        spec = self._behaviour.get(collection_name)
        if spec is None:
            raise UnexpectedResponse(
                status_code=404,
                reason_phrase="Not Found",
                content=b"",
                headers=None,  # type: ignore[arg-type]
            )
        if isinstance(spec, Exception):
            raise spec
        pages: list[list[_Rec]] = spec
        idx = 0 if offset is None else int(offset)
        page = pages[idx]
        next_offset = str(idx + 1) if idx + 1 < len(pages) else None
        return page, next_offset


def _run(monkeypatch: pytest.MonkeyPatch, behaviour: dict[str, Any], *args: str) -> Any:
    fake = FakeQdrant(behaviour)
    monkeypatch.setattr("musubi.cli.validate.QdrantClient", lambda **_: fake)
    return runner.invoke(app, ["validate", "rows", *args])


ALL_EMPTY: dict[str, Any] = {collection: [[]] for collection, _ in _PLANE_MODELS.values()}


def test_every_canonical_collection_is_swept() -> None:
    """The sweep must cover EVERY collection `store/names.py` declares.

    The first cut covered five of seven — it silently omitted `musubi_artifact_chunks`
    and `musubi_lifecycle_events`, both of which have strict payload models and active
    read paths. An integrity gate that skips two collections still prints `clean`, and
    that is the most dangerous direction for it to be wrong in.

    This test exists so the mapping cannot fall behind `names.py` again: add a collection
    there and this fails until you add it here.
    """
    from musubi.store.names import _PLANE_TO_COLLECTION

    swept = {collection for collection, _ in _PLANE_MODELS.values()}
    canonical = set(_PLANE_TO_COLLECTION.values()) | {"musubi_artifact_chunks"}
    missing = canonical - swept
    assert not missing, f"these canonical collections are NOT swept: {sorted(missing)}"


# ---------------------------------------------------------------------------
# THE BLOCKER: an instrument must not claim health it did not verify
# ---------------------------------------------------------------------------


def test_total_scan_failure_is_never_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every plane errors. Nothing is scanned. This must NOT report clean.

    Before the rev2 fix this printed `clean — 0 rows scanned` and exited 0, because
    `all_broken` was empty. An empty findings list from a scan that never happened is
    not evidence of health; it is the absence of evidence.
    """
    boom = ConnectionError("qdrant unreachable")
    result = _run(monkeypatch, dict.fromkeys(ALL_EMPTY, boom))

    assert result.exit_code == EXIT_INCOMPLETE, "a total scan failure must not exit 0"
    assert "clean —" not in result.stdout, "must not print the success line"
    assert "INCOMPLETE" in result.stdout
    assert "UNKNOWN" in result.stdout


def test_partial_scan_failure_is_never_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """Four planes scan fine, one fails. The run is still INCOMPLETE.

    This is the seductive case: real rows scanned, real zero findings — and a plane
    nobody looked at. Partial results must never read as total.
    """
    behaviour: dict[str, Any] = dict(ALL_EMPTY)
    behaviour["musubi_episodic"] = [[_Rec(GOOD_EPISODIC)]]
    behaviour["musubi_curated"] = ConnectionError("auth expired mid-sweep")

    result = _run(monkeypatch, behaviour)
    assert result.exit_code == EXIT_INCOMPLETE
    assert "clean —" not in result.stdout, "must not print the success line"
    assert "curated" in result.stdout
    assert "SCAN FAILED" in result.stdout


def test_failure_partway_through_pagination_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Page 1 scans, page 2 explodes. We saw SOME rows — that is the trap."""

    class HalfBroken(FakeQdrant):
        def scroll(self, *, collection_name: str, limit: int, offset: Any, **kw: Any) -> Any:
            if collection_name == "musubi_episodic" and offset is not None:
                raise ConnectionError("connection reset on page 2")
            if collection_name == "musubi_episodic":
                return [_Rec(GOOD_EPISODIC)], "1"
            return super().scroll(collection_name=collection_name, limit=limit, offset=offset)

    fake = HalfBroken(ALL_EMPTY)
    monkeypatch.setattr("musubi.cli.validate.QdrantClient", lambda **_: fake)
    result = runner.invoke(app, ["validate", "rows"])

    assert result.exit_code == EXIT_INCOMPLETE
    assert "clean —" not in result.stdout, "must not print the success line"


def test_json_reports_every_plane_including_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON must not omit the planes that failed — that is how a consumer gets misled."""
    behaviour: dict[str, Any] = dict(ALL_EMPTY)
    behaviour["musubi_curated"] = ConnectionError("down")

    result = _run(monkeypatch, behaviour, "--json")
    doc = json.loads(result.stdout)

    assert doc["complete"] is False
    assert doc["verdict"] == "incomplete"
    assert {p["plane"] for p in doc["planes"]} == set(_PLANE_MODELS), (
        "every requested plane must appear in the JSON, especially the ones that failed — "
        "omitting them is how a consumer gets misled into reading a partial sweep as total"
    )
    curated = next(p for p in doc["planes"] if p["plane"] == "curated")
    assert curated["status"] == "error"
    assert curated["error"]
    assert result.exit_code == EXIT_INCOMPLETE


# ---------------------------------------------------------------------------
# Clean / broken / absent
# ---------------------------------------------------------------------------


def test_clean_run_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    behaviour = dict(ALL_EMPTY)
    behaviour["musubi_episodic"] = [[_Rec(GOOD_EPISODIC)]]

    result = _run(monkeypatch, behaviour)
    assert result.exit_code == 0
    assert "clean" in result.stdout
    assert "1 rows scanned" in result.stdout or "rows scanned" in result.stdout


def test_broken_row_is_reported_with_the_offending_key(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = dict(GOOD_EPISODIC, retracted_original="the key that bricks the row")
    behaviour = dict(ALL_EMPTY)
    behaviour["musubi_episodic"] = [[_Rec(bad)]]

    result = _run(monkeypatch, behaviour)
    assert result.exit_code == 1, "one broken row → exit 1"
    assert "UNREADABLE" in result.stdout
    assert "retracted_original" in result.stdout
    assert "3GJhJLAvYXzIp8Qe8tuPHR9S9th" in result.stdout


def test_absent_collection_is_not_an_error_and_not_a_lie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plane never bootstrapped on this cluster is `absent` — benign, and distinct
    from both `clean` and `error`. It must not be silently folded into success."""
    behaviour = dict(ALL_EMPTY)
    del behaviour["musubi_thought"]  # 404 → absent

    result = _run(monkeypatch, behaviour, "--json")
    doc = json.loads(result.stdout)
    thought = next(p for p in doc["planes"] if p["plane"] == "thought")
    assert thought["status"] == "absent"
    assert doc["complete"] is True, "an absent collection does not make the run incomplete"
    assert result.exit_code == 0


def test_pagination_walks_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = dict(GOOD_EPISODIC, object_id="secondpage", retracted_original="x")
    behaviour = dict(ALL_EMPTY)
    # The broken row is on page 2 — a sweep that stops at page 1 would report clean.
    behaviour["musubi_episodic"] = [[_Rec(GOOD_EPISODIC)], [_Rec(bad, "p2")]]

    result = _run(monkeypatch, behaviour)
    assert result.exit_code == 1
    assert "secondpage" in result.stdout


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_single_plane_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    behaviour = dict(ALL_EMPTY)
    behaviour["musubi_episodic"] = [[_Rec(GOOD_EPISODIC)]]

    result = _run(monkeypatch, behaviour, "--plane", "episodic", "--json")
    doc = json.loads(result.stdout)
    assert [p["plane"] for p in doc["planes"]] == ["episodic"]
    assert result.exit_code == 0


def test_invalid_plane_is_rejected_before_touching_qdrant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown plane is a usage error, not an empty sweep. It must not exit 0, and it
    must not reach Qdrant — a typo'd --plane that quietly scanned nothing and reported
    clean would be the same lie in a different costume."""
    fake = FakeQdrant(ALL_EMPTY)
    monkeypatch.setattr("musubi.cli.validate.QdrantClient", lambda **_: fake)
    result = runner.invoke(app, ["validate", "rows", "--plane", "nonsense"])

    assert result.exit_code != 0
    assert fake.scroll_calls == 0, "must reject an unknown plane before scanning anything"


def test_nonpositive_batch_is_rejected_before_touching_qdrant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeQdrant(ALL_EMPTY)
    monkeypatch.setattr("musubi.cli.validate.QdrantClient", lambda **_: fake)
    result = runner.invoke(app, ["validate", "rows", "--batch", "0"])

    assert result.exit_code != 0
    assert fake.scroll_calls == 0, "must reject bad input before calling Qdrant"
