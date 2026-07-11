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
from musubi.cli.validate import (
    _PLANE_MODELS,
    EXIT_BROKEN_PARTIAL,
    EXIT_CLEAN_PARTIAL,
    EXIT_INCOMPLETE,
)

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
    from musubi.store.names import COLLECTION_NAMES

    swept = {collection for collection, _ in _PLANE_MODELS.values()}
    missing = set(COLLECTION_NAMES) - swept
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
    assert "VERDICT: clean" in result.stdout
    assert "COVERAGE — FULL" in result.stdout
    assert "INTEGRITY — CLEAN" in result.stdout


def test_broken_row_is_reported_with_the_offending_key(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = dict(GOOD_EPISODIC, retracted_original="the key that bricks the row")
    behaviour = dict(ALL_EMPTY)
    behaviour["musubi_episodic"] = [[_Rec(bad)]]

    result = _run(monkeypatch, behaviour)
    assert result.exit_code == 1, "one broken row → exit 1"
    assert "UNREADABLE" in result.stdout
    assert "retracted_original" in result.stdout
    assert "3GJhJLAvYXzIp8Qe8tuPHR9S9th" in result.stdout


def test_all_collections_missing_is_never_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point at the WRONG QDRANT NODE and every collection 404s. This must not say clean.

    This is the test that was here before, inverted. The old one was called
    `test_absent_collection_is_not_an_error_and_not_a_lie` and it asserted
    `complete is True` and exit 0 — it LOCKED IN the lie, under a name that claimed the
    opposite. With every canonical collection absent the command printed
    `clean — 0 rows scanned across 7 plane(s)`.

    A missing canonical collection is not an empty plane. `store/names.py` declares all
    seven canonical and `store/collections.py` bootstraps every one, so absence means an
    unbootstrapped, damaged, or wrong node. That is not evidence production is clean; it
    is evidence we are not looking at production. (Yua, rev3 review.)
    """
    result = _run(monkeypatch, {})  # nothing exists → every scroll 404s

    assert result.exit_code == EXIT_INCOMPLETE, "a wrong/empty node must never exit 0"
    assert "clean —" not in result.stdout
    assert "INCOMPLETE" in result.stdout
    assert "WRONG Qdrant node" in result.stdout


def test_one_missing_canonical_collection_is_never_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six planes scan clean, one collection is missing. Still not a production pass."""
    behaviour = dict(ALL_EMPTY)
    del behaviour["musubi_thought"]

    result = _run(monkeypatch, behaviour)
    assert result.exit_code == EXIT_INCOMPLETE
    assert "clean —" not in result.stdout
    assert "CANONICAL COLLECTION MISSING" in result.stdout


def test_allow_absent_is_clean_partial_never_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """The permissive escape hatch exists for a knowingly fresh cluster — and even then it
    must NOT be able to produce the same verdict as a fully scanned clean run."""
    behaviour = dict(ALL_EMPTY)
    del behaviour["musubi_thought"]

    result = _run(monkeypatch, behaviour, "--allow-absent", "--json")
    doc = json.loads(result.stdout)

    assert doc["verdict"] == "clean-partial", "must be distinguishable from a real clean run"
    assert doc["coverage"] == "partial"
    assert doc["integrity"] == "clean"
    # `complete` means FULL coverage. Accepting an absence does not make it full.
    assert doc["complete"] is False, "an accepted absence is still not complete coverage"
    assert doc["absent_collections"] == ["musubi_thought"]
    assert result.exit_code == EXIT_CLEAN_PARTIAL


# ---------------------------------------------------------------------------
# The mixed-result contract: coverage and integrity are INDEPENDENT
#
# Folding them together made the command contradict itself in adjacent sentences:
# `clean-partial ... every one readable by its model` printed directly above
# `1 of 1 scanned rows are UNREADABLE`. And `--allow-absent` set `complete: true` on a run
# that had skipped a canonical collection. (Yua, rev4 review of PR #398.)
# ---------------------------------------------------------------------------


def test_allow_absent_with_broken_rows_is_broken_partial_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence accepted AND a broken row found. Both facts must survive, separately."""
    bad = dict(GOOD_EPISODIC, retracted_original="bricks the row")
    behaviour = dict(ALL_EMPTY)
    behaviour["musubi_episodic"] = [[_Rec(bad)]]
    del behaviour["musubi_thought"]

    result = _run(monkeypatch, behaviour, "--allow-absent", "--json")
    doc = json.loads(result.stdout)

    assert doc["verdict"] == "broken-partial"
    assert doc["coverage"] == "partial", "a skipped canonical collection is not full coverage"
    assert doc["integrity"] == "broken"
    assert doc["complete"] is False, "MUST NOT claim complete coverage — a collection was skipped"
    assert doc["absent_collections"] == ["musubi_thought"]
    assert doc["broken_total"] == 1
    assert result.exit_code == EXIT_BROKEN_PARTIAL, (
        "broken-partial must not collapse into an ordinary fully-scanned broken count — "
        "'2 bad rows, saw everything' and '2 bad rows, skipped a collection' are different facts"
    )


def test_allow_absent_with_broken_rows_never_claims_rows_are_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The human output must not say every scanned row is readable while listing unreadable
    rows two lines later. It literally did."""
    bad = dict(GOOD_EPISODIC, retracted_original="bricks the row")
    behaviour = dict(ALL_EMPTY)
    behaviour["musubi_episodic"] = [[_Rec(bad)]]
    del behaviour["musubi_thought"]

    result = _run(monkeypatch, behaviour, "--allow-absent")
    out = result.stdout

    assert "VERDICT: broken-partial" in out
    assert "every one readable" not in out, "must never claim readability while rows are broken"
    assert "INTEGRITY — BROKEN" in out
    assert "COVERAGE — PARTIAL" in out
    assert "UNREADABLE" in out
    assert result.exit_code == EXIT_BROKEN_PARTIAL


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


def test_single_plane_can_never_emit_the_production_pass_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--plane episodic` scans 1 of 7 canonical collections. It must NOT look like a pass.

    This test is the previous one inverted. `test_single_plane_filter` asserted exit 0 —
    so a narrowed scope emitted `coverage: full`, `complete: true`, `verdict: clean`,
    exit 0: **the exact machine signal of a clean full-production sweep**, while six
    canonical collections were never even requested. It locked the false pass in.

    Same coverage-denominator defect as accepted absence, wearing a scope flag. "Full
    relative to what I selected" is not "full relative to production." (Yua, rev5 review.)
    """
    behaviour = dict(ALL_EMPTY)
    behaviour["musubi_episodic"] = [[_Rec(GOOD_EPISODIC)]]

    result = _run(monkeypatch, behaviour, "--plane", "episodic", "--json")
    doc = json.loads(result.stdout)

    assert result.exit_code == EXIT_CLEAN_PARTIAL, "a one-plane run must never exit 0"
    assert doc["verdict"] == "clean-partial"
    assert doc["coverage"] == "partial"
    assert doc["complete"] is False, "1 of 7 collections is not complete coverage"
    assert doc["scope"] == "selected"

    # The denominator must be explicit — a consumer must never have to infer it.
    assert set(doc["canonical_collections"]) == {c for c, _ in _PLANE_MODELS.values()}
    assert doc["requested_collections"] == ["musubi_episodic"]
    assert len(doc["not_requested_collections"]) == len(_PLANE_MODELS) - 1

    # Every canonical plane still appears in the report, marked not_requested.
    assert {p["plane"] for p in doc["planes"]} == set(_PLANE_MODELS)
    ep = next(p for p in doc["planes"] if p["plane"] == "episodic")
    assert ep["status"] == "scanned"


def test_single_plane_with_broken_rows_is_broken_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A narrowed scope that finds a bad row is broken-PARTIAL, not an ordinary broken run."""
    bad = dict(GOOD_EPISODIC, retracted_original="bricks it")
    behaviour = dict(ALL_EMPTY)
    behaviour["musubi_episodic"] = [[_Rec(bad)]]

    result = _run(monkeypatch, behaviour, "--plane", "episodic", "--json")
    doc = json.loads(result.stdout)

    assert doc["verdict"] == "broken-partial"
    assert doc["complete"] is False
    assert result.exit_code == EXIT_BROKEN_PARTIAL, (
        "must not collapse into a fully-scanned broken count — six collections were unseen"
    )


def test_default_run_is_canonical_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the default, all-seven, fully-scanned, clean run may exit 0."""
    behaviour = dict(ALL_EMPTY)
    behaviour["musubi_episodic"] = [[_Rec(GOOD_EPISODIC)]]

    result = _run(monkeypatch, behaviour, "--json")
    doc = json.loads(result.stdout)

    assert doc["scope"] == "canonical"
    assert doc["coverage"] == "full"
    assert doc["complete"] is True
    assert doc["not_requested_collections"] == []
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
