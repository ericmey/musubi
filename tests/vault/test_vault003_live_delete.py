"""VAULT-003: live vault delete must archive the curated row (Issue #552).

Owner slice: slice-vault003-live-delete (#552).

Discriminating contract: a live filesystem delete event from
``VaultWatcher._handle_deleted`` resolves the matching curated row by
its STORED ``vault_path`` (NOT by re-reading the absent file), then
archives it through the canonical
``CuratedPlane.transition(... coordinator=...)`` seam. No raw
Qdrant mutation, no second lifecycle path.

Identity resolution lives on ``CuratedPlane.find_by_vault_path`` as a
typed public method; the watcher does NOT scroll Qdrant directly.
The vault-path lookup is exact equality on ``payload.vault_path``
(NOT startswith / prefix / regex), so sibling and prefix-collision
paths cannot match by construction.

Idempotency carve-out: a repeat delete on an already-archived row
catches ONLY ``TransitionError(code='illegal_transition', to_state=
'archived')`` and treats it as success. Every other error code
(``version_fence_violation``, ``not_found``, ``terminal_apply_failure``,
``lifecycle_event_write_failed``, ``invariant_violation``, etc.) is
logged at ``warning`` level with structured fields and the handler
returns — no in-handler retry, to avoid unbounded recursion.

The tests in this file transcribe the slice's Test Contract bullets
verbatim — RED discriminators that must fail under pre-slice code, plus
GREEN preservation guards that must keep passing — per the AGENTS.md
Test Contract Closure Rule. The slice doc's Test Contract is the single
source of truth for the enumerated cases and their count; this docstring
deliberately does not restate a number that would drift as the contract
evolves (later review rounds have already added cases).

    uv run pytest tests/vault/test_vault003_live_delete.py -v
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import QdrantClient
from watchdog.events import FileCreatedEvent, FileDeletedEvent

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.lifecycle.events import LifecycleEventSink
from musubi.lifecycle.transitions import TransitionError
from musubi.planes.curated import CuratedPlane
from musubi.types.common import Err, Ok
from musubi.types.curated import CuratedKnowledge
from musubi.vault.frontmatter import CuratedFrontmatter, dump_frontmatter
from musubi.vault.watcher import VaultWatcher
from musubi.vault.writelog import WriteLog

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _make_curated(
    *,
    namespace: str,
    vault_path: str,
    content: str,
    title: str = "VAULT-003 fixture",
    topics: list[str] | None = None,
    **extra: Any,
) -> CuratedKnowledge:
    """Build a :class:`CuratedKnowledge` for the watcher tests."""
    return CuratedKnowledge(
        namespace=namespace,
        title=title,
        content=content,
        vault_path=vault_path,
        body_hash=_hash(content),
        topics=topics or ["vault/delete-test"],
        **extra,
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    from musubi.store import bootstrap

    client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def plane(qdrant: QdrantClient) -> CuratedPlane:
    return CuratedPlane(client=qdrant, embedder=FakeEmbedder())


@pytest.fixture
def ns() -> str:
    return "eric/claude-code/curated"


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "eric" / "shared").mkdir(parents=True)
    return root


@pytest.fixture
def write_log(tmp_path: Path) -> WriteLog:
    return WriteLog(tmp_path / "write-log.sqlite")


@pytest.fixture
def sink(tmp_path: Path) -> LifecycleEventSink:
    """A sink that reads the SAME sqlite DB the coordinator writes to
    (``lifecycle_events`` lives in the coordinator's db file)."""
    return LifecycleEventSink(db_path=tmp_path / "coord.db")


@pytest.fixture
def coordinator(qdrant: QdrantClient, tmp_path: Path) -> LifecycleTransitionCoordinator:
    """A real coordinator wired against the in-memory Qdrant + tmp_path sqlite."""
    return LifecycleTransitionCoordinator(
        client=qdrant,
        db_path=tmp_path / "coord.db",
    )


@pytest.fixture
async def watcher(
    vault_root: Path,
    plane: CuratedPlane,
    write_log: WriteLog,
    coordinator: LifecycleTransitionCoordinator,
) -> VaultWatcher:
    """A watcher wired against the canonical coordinator seam.

    ``debounce_sec=0.1`` keeps tests fast; the watchdog observer is
    not started (we drive ``_handle_event`` directly).
    """
    w = VaultWatcher(
        vault_root=vault_root,
        curated_plane=plane,
        write_log=write_log,
        coordinator=coordinator,
        debounce_sec=0.1,
    )
    w._loop = asyncio.get_running_loop()
    return w


# --------------------------------------------------------------------------- #
# RED discriminating tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delete_archives_matching_row_via_canonical_transition(
    plane: CuratedPlane,
    ns: str,
    watcher: VaultWatcher,
) -> None:
    """Bullet 1: delete resolves to exact stored ``vault_path``; the row's
    ``state`` transitions to ``'archived'`` through the canonical
    coordinator (NOT raw ``set_payload``)."""
    saved = await plane.create(
        _make_curated(
            namespace=ns,
            vault_path="eric/shared/deleted-target.md",
            content="This file's vault_path will be deleted.",
        )
    )
    rel_path = "eric/shared/deleted-target.md"
    await watcher._handle_event(
        str(watcher.vault_root / rel_path),
        FileDeletedEvent(str(watcher.vault_root / rel_path)),
    )
    after = await plane.get(namespace=ns, object_id=saved.object_id)
    assert after is not None
    assert after.state == "archived"


@pytest.mark.asyncio
async def test_archived_row_excluded_from_default_retrieval(
    plane: CuratedPlane,
    ns: str,
    watcher: VaultWatcher,
) -> None:
    """Bullet 2: post-archive, the curated default-retrieval path
    (``CuratedPlane.query``) does NOT return the archived row, and
    the row remains readable by ``object_id`` via
    :meth:`CuratedPlane.get` (the audit/history path).

    This is the Yua-review-corrected (round-4) version: the previous
    raw ``plane._client.scroll(scroll_filter=None)`` assertion
    could not actually prove the row was excluded from the default
    retrieval path because the default path is
    ``CuratedPlane.query(...)`` which filters on
    ``state in ("matured",)``. This test now drives the default
    path directly and asserts the archived row is absent from its
    result while still readable by id.
    """
    saved = await plane.create(
        _make_curated(
            namespace=ns,
            vault_path="eric/shared/default-excluded.md",
            content="Default retrieval should not surface this row after archive.",
        )
    )
    rel_path = "eric/shared/default-excluded.md"
    await watcher._handle_event(
        str(watcher.vault_root / rel_path),
        FileDeletedEvent(str(watcher.vault_root / rel_path)),
    )

    # The actual default retrieval path: ``CuratedPlane.query`` filters
    # on ``state in ("matured",)`` and namespace + valid_at. The
    # archived row must NOT be in the result.
    default_view = await plane.query(namespace=ns, query="default excluded")
    default_view_ids = {row.object_id for row in default_view}
    assert saved.object_id not in default_view_ids, (
        f"archived row {saved.object_id} must NOT appear in the "
        f"default retrieval view; got {default_view_ids!r}"
    )
    assert all(row.state == "matured" for row in default_view), (
        f"default view must contain ONLY matured rows; got "
        f"states={[row.state for row in default_view]!r}"
    )

    # The row is still readable by id (audit/history retention) — the
    # ``get`` path is state-agnostic.
    fetched = await plane.get(namespace=ns, object_id=saved.object_id)
    assert fetched is not None
    assert fetched.state == "archived"
    assert fetched.object_id == saved.object_id


@pytest.mark.asyncio
async def test_audit_and_history_retain_archived_row(
    plane: CuratedPlane,
    ns: str,
    watcher: VaultWatcher,
    sink: LifecycleEventSink,
) -> None:
    """Bullet 3: the ``lifecycle_events`` table contains a row with
    ``reason='vault file deleted: ...'``, ``target_state='archived'``,
    ``actor='vault-watcher'``."""
    saved = await plane.create(
        _make_curated(
            namespace=ns,
            vault_path="eric/shared/audit-target.md",
            content="This row's delete should produce a lifecycle_events row.",
        )
    )
    rel_path = "eric/shared/audit-target.md"
    await watcher._handle_event(
        str(watcher.vault_root / rel_path),
        FileDeletedEvent(str(watcher.vault_root / rel_path)),
    )
    events = sink.read_all()
    matching = [
        e
        for e in events
        if e.to_state == "archived"
        and e.object_id == saved.object_id
        and e.actor == "vault-watcher"
        and e.reason == f"vault file deleted: {rel_path}"
    ]
    assert len(matching) == 1, (
        f"expected 1 lifecycle_events row matching the vault-delete pattern, "
        f"got {len(matching)} from {len(events)} total events"
    )


@pytest.mark.asyncio
async def test_repeat_delete_is_idempotent(
    plane: CuratedPlane,
    ns: str,
    watcher: VaultWatcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bullet 4: a second delete on an already-archived row returns
    ``illegal_transition`` from the canonical state machine; the watcher
    treats that single error code as success (no warning, no mutation,
    no retry)."""
    saved = await plane.create(
        _make_curated(
            namespace=ns,
            vault_path="eric/shared/idempotent.md",
            content="Repeat delete should not log a warning or re-archive.",
        )
    )
    rel_path = "eric/shared/idempotent.md"
    abs_path = str(watcher.vault_root / rel_path)
    # First delete: archives.
    await watcher._handle_event(abs_path, FileDeletedEvent(abs_path))
    after_first = await plane.get(namespace=ns, object_id=saved.object_id)
    assert after_first is not None and after_first.state == "archived"

    # Second delete: should be idempotent (no warning, state unchanged).
    with caplog.at_level(logging.WARNING, logger="musubi.vault.watcher"):
        await watcher._handle_event(abs_path, FileDeletedEvent(abs_path))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings, (
        f"idempotent repeat-delete should not emit warnings, got: "
        f"{[r.getMessage() for r in warnings]}"
    )
    after_second = await plane.get(namespace=ns, object_id=saved.object_id)
    assert after_second is not None
    assert after_second.state == "archived"
    assert after_second.version == after_first.version, "version must not bump on idempotent repeat"


@pytest.mark.asyncio
async def test_sibling_path_does_not_archive_target(
    plane: CuratedPlane,
    ns: str,
    watcher: VaultWatcher,
) -> None:
    """Bullet 5: delete ``foo/bar.md``; ``foo/bar-2.md`` and ``foo/bar.md.bak``
    both remain in ``matured``. The exact-match lookup excludes both."""
    target = await plane.create(
        _make_curated(
            namespace=ns,
            vault_path="foo/bar.md",
            content="Exact target.",
        )
    )
    sibling_dash = await plane.create(
        _make_curated(
            namespace=ns,
            vault_path="foo/bar-2.md",
            content="Sibling with dash suffix.",
        )
    )
    sibling_dot = await plane.create(
        _make_curated(
            namespace=ns,
            vault_path="foo/bar.md.bak",
            content="Sibling with .bak suffix.",
        )
    )
    rel_path = "foo/bar.md"
    abs_path = str(watcher.vault_root / rel_path)
    await watcher._handle_event(abs_path, FileDeletedEvent(abs_path))

    target_after = await plane.get(namespace=ns, object_id=target.object_id)
    sibling_dash_after = await plane.get(namespace=ns, object_id=sibling_dash.object_id)
    sibling_dot_after = await plane.get(namespace=ns, object_id=sibling_dot.object_id)
    assert target_after is not None and target_after.state == "archived"
    assert sibling_dash_after is not None and sibling_dash_after.state == "matured"
    assert sibling_dot_after is not None and sibling_dot_after.state == "matured"


@pytest.mark.asyncio
async def test_prefix_collision_does_not_archive(
    plane: CuratedPlane,
    ns: str,
    watcher: VaultWatcher,
) -> None:
    """Bullet 6: delete ``dir/sub/file.md``; ``dir/subfile.md`` and
    ``dir/sub2/file.md`` remain in ``matured``. Exact equality excludes
    both."""
    target = await plane.create(
        _make_curated(
            namespace=ns,
            vault_path="dir/sub/file.md",
            content="Deep target.",
        )
    )
    sibling_no_slash = await plane.create(
        _make_curated(
            namespace=ns,
            vault_path="dir/subfile.md",
            content="Sibling without separator.",
        )
    )
    sibling_deeper_dir = await plane.create(
        _make_curated(
            namespace=ns,
            vault_path="dir/sub2/file.md",
            content="Sibling in adjacent dir.",
        )
    )
    rel_path = "dir/sub/file.md"
    abs_path = str(watcher.vault_root / rel_path)
    await watcher._handle_event(abs_path, FileDeletedEvent(abs_path))

    target_after = await plane.get(namespace=ns, object_id=target.object_id)
    sibling_no_slash_after = await plane.get(namespace=ns, object_id=sibling_no_slash.object_id)
    sibling_deeper_dir_after = await plane.get(namespace=ns, object_id=sibling_deeper_dir.object_id)
    assert target_after is not None and target_after.state == "archived"
    assert sibling_no_slash_after is not None and sibling_no_slash_after.state == "matured"
    assert sibling_deeper_dir_after is not None and sibling_deeper_dir_after.state == "matured"


@pytest.mark.asyncio
async def test_missing_row_is_observable_noop(
    plane: CuratedPlane,
    ns: str,
    watcher: VaultWatcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bullet 7: delete a path with no curated row; an ``info``-level log
    records the path; no mutation, no error, no warning."""
    rel_path = "eric/shared/never-existed.md"
    abs_path = str(watcher.vault_root / rel_path)
    with caplog.at_level(logging.DEBUG, logger="musubi.vault.watcher"):
        await watcher._handle_event(abs_path, FileDeletedEvent(abs_path))

    # At least one info-level record mentions the missing path so operators
    # see the event landed.
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    info_paths = [r.getMessage() for r in info_records]
    assert any(rel_path in m for m in info_paths), (
        f"expected info log mentioning {rel_path!r}, got: {info_paths}"
    )
    # No warning on the missing-row path (no fence failure, no error).
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warning_records, (
        f"missing-row no-op must not emit warnings, got: "
        f"{[r.getMessage() for r in warning_records]}"
    )
    # No mutation: the curated plane still has zero rows.
    records, _ = plane._client.scroll(
        collection_name=plane._collection,
        limit=10,
        with_payload=["object_id"],
        with_vectors=False,
    )
    assert records == []


@pytest.mark.asyncio
async def test_transition_failure_remains_visible(
    plane: CuratedPlane,
    ns: str,
    coordinator: LifecycleTransitionCoordinator,
    sink: LifecycleEventSink,
    vault_root: Path,
    write_log: WriteLog,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bullet 8: a forced transition failure (simulated version_fence_violation)
    logs a structured warning with ``code``, ``message``, ``path``. No
    retry. No success-log. The row's state is unchanged."""
    # Patch the canonical transition seam to raise a fenced error once.
    real_transition = plane.transition
    captured: dict[str, Any] = {}

    async def _failing_transition(*args: Any, **kwargs: Any) -> Any:
        captured["kwargs"] = kwargs
        return Err(
            error=TransitionError(
                code="version_fence_violation",
                message="simulated concurrent maturation",
                to_state="archived",
            )
        )

    plane.transition = _failing_transition  # type: ignore[method-assign]
    try:
        saved = await plane.create(
            _make_curated(
                namespace=ns,
                vault_path="eric/shared/fenced.md",
                content="This row's transition will fail.",
            )
        )
        w = VaultWatcher(
            vault_root=vault_root,
            curated_plane=plane,
            write_log=write_log,
            coordinator=coordinator,
            debounce_sec=0.1,
        )
        w._loop = asyncio.get_running_loop()

        rel_path = "eric/shared/fenced.md"
        abs_path = str(vault_root / rel_path)

        with caplog.at_level(logging.WARNING, logger="musubi.vault.watcher"):
            await w._handle_event(abs_path, FileDeletedEvent(abs_path))

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "transition failure must emit at least one warning"
        joined = " ".join(r.getMessage() for r in warnings)
        assert "version_fence_violation" in joined, (
            f"warning must mention the error code, got: {joined!r}"
        )
        assert rel_path in joined, f"warning must mention the path, got: {joined!r}"

        # The row's state is unchanged (still matured) — the failing
        # transition did not commit.
        after = await plane.get(namespace=ns, object_id=saved.object_id)
        assert after is not None and after.state == "matured"
    finally:
        plane.transition = real_transition  # type: ignore[method-assign]


# --------------------------------------------------------------------------- #
# GREEN preservation guards (the seam must not break the existing sync tests)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_on_created_indexes_new_file(
    vault_root: Path,
    plane: CuratedPlane,
    write_log: WriteLog,
    coordinator: LifecycleTransitionCoordinator,
) -> None:
    """GREEN guard 9: the on-created path is unaffected by the seam."""
    from datetime import UTC, datetime

    file_path = vault_root / "eric" / "shared" / "test.md"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    fm = CuratedFrontmatter.model_validate(
        {
            "title": "Created file",
            "summary": "Body",
            "created": now,
            "updated": now,
        }
    )
    file_path.write_text(
        dump_frontmatter(fm.model_dump(mode="json", exclude_none=True), "Body content."),
        encoding="utf-8",
    )

    w = VaultWatcher(
        vault_root=vault_root,
        curated_plane=plane,
        write_log=write_log,
        coordinator=coordinator,
        debounce_sec=0.1,
    )
    w._loop = asyncio.get_running_loop()
    # Drive the on-created path with a real handle. The seam (delete
    # only) is not exercised here; this guard proves the new
    # constructor signature does not break the create path.
    await w._handle_event(str(file_path), FileCreatedEvent(str(file_path)))


@pytest.mark.asyncio
async def test_dotfile_ignored(
    vault_root: Path,
    plane: CuratedPlane,
    write_log: WriteLog,
    coordinator: LifecycleTransitionCoordinator,
) -> None:
    """GREEN guard 10: dotfile paths are still ignored — the seam does
    not perturb the pre-existing debouncer / event-ingest path."""
    file_path = vault_root / ".ignored.md"
    file_path.write_text("Title: ignored", encoding="utf-8")

    w = VaultWatcher(
        vault_root=vault_root,
        curated_plane=plane,
        write_log=write_log,
        coordinator=coordinator,
        debounce_sec=0.1,
    )
    w._loop = asyncio.get_running_loop()
    w.enqueue_event(FileCreatedEvent(str(file_path)))
    await asyncio.sleep(0.05)
    assert len(w._pending_tasks) == 0


# --------------------------------------------------------------------------- #
# RED discriminating tests — Yua review follow-up (2026-07-15 16:48)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_two_namespaces_same_vault_path_neither_archives(
    plane: CuratedPlane,
    coordinator: LifecycleTransitionCoordinator,
    sink: LifecycleEventSink,
    write_log: WriteLog,
    vault_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bullet 11 (Blocker 2): two rows in DIFFERENT namespaces with the
    SAME ``vault_path`` both remain in ``matured`` after a delete event
    for that path. The watcher fails closed + visibly on the
    multiple-matches case; it does NOT archive an arbitrary row.

    Pre-Yua-review: the public ``find_by_vault_path`` scrolled
    ``limit=1`` and returned the first match, so the watcher's
    archive transition would target an arbitrary one of the two rows
    (whichever Qdrant returned first). The seam now fails closed with
    a structured warning listing both object_ids."""
    # Two distinct namespaces, same vault_path.
    row_a = await plane.create(
        _make_curated(
            namespace="tenant-a/agent-x/curated",
            vault_path="shared/conflict.md",
            content="Tenant A's copy of the conflicting path.",
        )
    )
    row_b = await plane.create(
        _make_curated(
            namespace="tenant-b/agent-y/curated",
            vault_path="shared/conflict.md",
            content="Tenant B's copy of the conflicting path.",
        )
    )
    w = VaultWatcher(
        vault_root=vault_root,
        curated_plane=plane,
        write_log=write_log,
        coordinator=coordinator,
        debounce_sec=0.1,
    )
    w._loop = asyncio.get_running_loop()
    rel_path = "shared/conflict.md"
    abs_path = str(vault_root / rel_path)

    with caplog.at_level(logging.WARNING, logger="musubi.vault.watcher"):
        await w._handle_event(abs_path, FileDeletedEvent(abs_path))

    # Both rows must remain in their original state.
    after_a = await plane.get(namespace=row_a.namespace, object_id=row_a.object_id)
    after_b = await plane.get(namespace=row_b.namespace, object_id=row_b.object_id)
    assert after_a is not None and after_a.state == "matured"
    assert after_b is not None and after_b.state == "matured"

    # The watcher logs a structured warning naming BOTH object_ids.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("multiple-matches" in r.getMessage() for r in warnings), (
        f"expected a multiple-matches warning, got: {[r.getMessage() for r in warnings]}"
    )
    joined = " ".join(r.getMessage() for r in warnings)
    assert row_a.object_id in joined, f"warning must list row_a object_id, got: {joined!r}"
    assert row_b.object_id in joined, f"warning must list row_b object_id, got: {joined!r}"
    # The count must be presented as a truthful bounded LOWER BOUND, never as
    # an exact cardinality — the resolver caps its scroll at limit=2.
    assert "match_count_at_least=" in joined, (
        f"warning must present the count as a lower bound (match_count_at_least=), "
        f"not an exact match_count; got: {joined!r}"
    )
    assert "match_count=" not in joined, (
        f"warning must NOT present an exact 'match_count=' (it is capped at 2, a "
        f"lower bound); got: {joined!r}"
    )


@pytest.mark.asyncio
async def test_superseded_row_delete_emits_visible_warning(
    plane: CuratedPlane,
    ns: str,
    watcher: VaultWatcher,
    coordinator: LifecycleTransitionCoordinator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bullet 12 (Blocker 3): a ``superseded`` row is NOT archived
    silently when the file is deleted. The watcher's idempotency
    carve-out was previously too permissive: any
    ``illegal_transition(to_state='archived')`` was treated as
    success, but a superseded row's transition to archived is ALSO
    ``illegal_transition`` (curated state table: ``superseded`` is
    terminal). Yua binding: require ``current.state == 'archived'``
    as the idempotency discriminator; wrong-state rows emit a
    visible warning, no archive, no idempotent-silent success.

    Setup: a row that is the ``superseded`` half of a
    (new, old) pair where the new row is the only one matching the
    vault_path. The current ``find_by_vault_path`` is
    cross-namespace, so the new row matches but the OLD superseded
    row is what the watcher is supposed to leave alone.

    Wait — the test name says 'superseded row', but the seam resolves
    by current stored vault_path, not by the lifecycle link. To
    exercise the wrong-state path the seam surfaces, we patch the
    canonical transition to simulate the rejection, then assert the
    structured warning is logged and the row's state is preserved.
    """
    saved = await plane.create(
        _make_curated(
            namespace=ns,
            vault_path="eric/shared/wrong-state.md",
            content="Row whose transition will be rejected.",
        )
    )

    # Patch the canonical transition seam to simulate a wrong-state
    # rejection (e.g. the row's real `from_state` is `superseded` and
    # the canonical state machine rejects `superseded -> archived`).
    real_transition = plane.transition
    called: dict[str, Any] = {}

    async def _rejecting_transition(*args: Any, **kwargs: Any) -> Any:
        called["kwargs"] = kwargs
        from musubi.lifecycle.transitions import TransitionError

        return Err(
            error=TransitionError(
                code="illegal_transition",
                message="simulated: superseded -> archived is not permitted",
                from_state="superseded",
                to_state="archived",
            )
        )

    plane.transition = _rejecting_transition  # type: ignore[method-assign]
    try:
        rel_path = "eric/shared/wrong-state.md"
        abs_path = str(watcher.vault_root / rel_path)

        with caplog.at_level(logging.WARNING, logger="musubi.vault.watcher"):
            await watcher._handle_event(abs_path, FileDeletedEvent(abs_path))
    finally:
        plane.transition = real_transition  # type: ignore[method-assign]

    # The seam was actually invoked (not short-circuited by the
    # archived state pre-check) — proves the wrong-state row is NOT
    # being treated as idempotent.
    assert called, f"the canonical transition seam must have been invoked; called={called!r}"

    # The row's state is unchanged (the failing transition did not
    # commit).
    after = await plane.get(namespace=ns, object_id=saved.object_id)
    assert after is not None
    assert after.state == "matured"

    # The watcher must emit a visible warning (NOT log a success).
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "wrong-state delete must emit at least one visible warning"
    joined = " ".join(r.getMessage() for r in warnings)
    assert "vault-delete-failed" in joined, (
        f"warning must be a 'vault-delete-failed' (not idempotent success), got: {joined!r}"
    )
    assert "illegal_transition" in joined, f"warning must include the error code, got: {joined!r}"
    assert "superseded" in joined, (
        f"warning must include the actual from_state ('superseded'), got: {joined!r}"
    )


@pytest.mark.asyncio
async def test_concurrent_archive_race_is_idempotent_not_warned(
    plane: CuratedPlane,
    ns: str,
    watcher: VaultWatcher,
    coordinator: LifecycleTransitionCoordinator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """H12 Copilot round-3 (Yua ruling 1): the concurrent-archive race.

    ``find_by_vault_path`` reads a non-archived row, but before the
    watcher issues its transition another actor archives the SAME row.
    The canonical transition then rejects ``archived -> archived`` with
    ``illegal_transition(from_state='archived')``. The end state is
    exactly what this delete wanted, so it MUST be an idempotent no-op
    (debug, NO warning) — the same outcome as the pre-read
    archived-state carve-out, just resolved one layer down.

    Discriminator vs test_superseded_row_delete_emits_visible_warning:
    identical rejection shape EXCEPT ``from_state``. superseded -> WARN
    (real anomaly); archived -> DEBUG no-op (benign race). The pre-fix
    watcher warned on BOTH, so this test is RED before the fix.
    """
    saved = await plane.create(
        _make_curated(
            namespace=ns,
            vault_path="eric/shared/race-archived.md",
            content="Row archived by a concurrent actor mid-delete.",
        )
    )

    real_transition = plane.transition
    called: dict[str, Any] = {}

    async def _archived_race_transition(*args: Any, **kwargs: Any) -> Any:
        called["kwargs"] = kwargs
        from musubi.lifecycle.transitions import TransitionError

        return Err(
            error=TransitionError(
                code="illegal_transition",
                message="simulated race: archived -> archived is not permitted",
                from_state="archived",
                to_state="archived",
            )
        )

    plane.transition = _archived_race_transition  # type: ignore[method-assign]
    try:
        rel_path = "eric/shared/race-archived.md"
        abs_path = str(watcher.vault_root / rel_path)
        with caplog.at_level(logging.DEBUG, logger="musubi.vault.watcher"):
            await watcher._handle_event(abs_path, FileDeletedEvent(abs_path))
    finally:
        plane.transition = real_transition  # type: ignore[method-assign]

    # The seam was actually invoked — the row was NOT short-circuited by
    # the archived-state pre-check (it was 'matured' at find time here),
    # so the idempotency decision came from the from_state='archived'
    # transition result, which is exactly the race path.
    assert called, f"the canonical transition seam must have been invoked; called={called!r}"

    # NO warning: the benign race must not page an operator.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings, (
        f"concurrent archived->archived race must NOT warn (idempotent no-op); got: "
        f"{[r.getMessage() for r in warnings]!r}"
    )

    # A visible DEBUG breadcrumb records the race for anyone reading logs.
    debug_msgs = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
    assert "vault-delete-idempotent-race" in debug_msgs, (
        f"race must log a 'vault-delete-idempotent-race' debug breadcrumb; got: {debug_msgs!r}"
    )
    # Sanity: the saved row still exists (the simulated reject committed
    # nothing; the real race would have it already archived by the peer).
    assert saved.object_id


@pytest.mark.asyncio
async def test_delete_success_log_distinguishes_pending_from_finalized(
    plane: CuratedPlane,
    ns: str,
    watcher: VaultWatcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """H12 Copilot round-4 (Yua ruling 3): the watcher success log must not
    conflate ``Ok(TransitionPending)`` (durable-accept admitted, NOT yet
    finalized) with a finalized ``TransitionResult``. Pending -> truthful
    ``outcome=pending`` carrying the stable operation_key + event_id;
    finalized -> ``outcome=finalized`` carrying the event_id. The pre-fix log
    emitted ``outcome=ok`` for BOTH, so the pending assertions are RED before
    the fix."""
    from musubi.lifecycle.coordinator import TransitionPending

    # --- Case A: durable-accept pending (simulated seam result) ---
    await plane.create(
        _make_curated(namespace=ns, vault_path="eric/shared/pending.md", content="pending row")
    )

    async def _pending_transition(*args: Any, **kwargs: Any) -> Any:
        return Ok(
            value=TransitionPending(operation_key="op-pending-001", event_id="ev-pending-001")
        )

    real_transition = plane.transition
    plane.transition = _pending_transition  # type: ignore[method-assign]
    try:
        rel = "eric/shared/pending.md"
        with caplog.at_level(logging.INFO, logger="musubi.vault.watcher"):
            await watcher._handle_event(
                str(watcher.vault_root / rel), FileDeletedEvent(str(watcher.vault_root / rel))
            )
    finally:
        plane.transition = real_transition  # type: ignore[method-assign]

    pending_logs = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    assert "outcome=pending" in pending_logs, (
        f"durable-accept must log outcome=pending (not outcome=ok); got {pending_logs!r}"
    )
    assert "op-pending-001" in pending_logs, "pending log must carry the stable operation_key"
    assert "ev-pending-001" in pending_logs, "pending log must carry the stable event_id"
    assert "outcome=ok" not in pending_logs, "the conflated pre-fix 'outcome=ok' must be gone"

    caplog.clear()

    # --- Case B: real finalized archive (through the live coordinator) ---
    saved = await plane.create(
        _make_curated(namespace=ns, vault_path="eric/shared/finalized.md", content="finalized row")
    )
    rel2 = "eric/shared/finalized.md"
    with caplog.at_level(logging.INFO, logger="musubi.vault.watcher"):
        await watcher._handle_event(
            str(watcher.vault_root / rel2), FileDeletedEvent(str(watcher.vault_root / rel2))
        )
    final_logs = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    assert "outcome=finalized" in final_logs, (
        f"a finalized archive must log outcome=finalized; got {final_logs!r}"
    )
    # And the finalized path actually committed the archive.
    after = await plane.get(namespace=ns, object_id=saved.object_id)
    assert after is not None and after.state == "archived"


# --------------------------------------------------------------------------- #
# VAULT-003 Blocker 1: LIVE REACHABILITY discriminator
# --------------------------------------------------------------------------- #


def test_systemd_module_command_reaches_construction() -> None:
    """VAULT-003 Blocker 1: the systemd unit
    ``deploy/systemd/musubi-vault-sync.service`` runs
    ``python -m musubi.vault.watcher``. Before this slice the
    ``watcher.py`` had no ``__main__`` and no production
    ``VaultWatcher`` construction outside tests, so the service
    exited without starting the watcher. This test imports the
    ``musubi.vault.watcher`` module (the exact path the systemd
    unit invokes) and proves the live-reachability shape:

    - the module exposes a callable ``main()`` and an
      ``if __name__ == \"__main__\"`` block;
    - the module's runtime factory module
      (``musubi.vault.runtime``) is importable and returns a
      populated ``VaultSyncRuntime`` bundle from a stubbed settings
      object;
    - a ``VaultWatcher`` can be constructed from the runtime
      bundle (i.e. the production wiring matches the constructor
      signature).

    This is an AST + import-level discriminator, not a behaviour
    test. It will pass if anyone wires the systemd command to a
    callable entrypoint; it will fail (or be import-blocked) if the
    entrypoint is removed.
    """
    import importlib
    import inspect

    # 1. The systemd command path is importable and exposes main().
    watcher_module = importlib.import_module("musubi.vault.watcher")
    assert hasattr(watcher_module, "main"), (
        "musubi.vault.watcher.main is required for the systemd "
        "ExecStart=python -m musubi.vault.watcher to reach construction"
    )
    assert callable(watcher_module.main)
    # 2. The module has a __main__ guard.
    src = inspect.getsource(watcher_module)
    assert '__name__ == "__main__"' in src or "__name__ == '__main__'" in src, (
        "watcher.py must have an `if __name__ == '__main__':` block "
        "for `python -m musubi.vault.watcher` to actually call main()"
    )
    # The __main__ block must actually CALL main(), not merely define it.
    # A bare ``"main()" in src`` is a false positive: it matches the
    # ``def main()`` definition above the guard even if the guard body never
    # calls it. Capture the guard's indented body and assert a main() call
    # lives inside it (Copilot PR #562).
    import re

    guard_match = re.search(
        r"if\s+__name__\s*==\s*['\"]__main__['\"]\s*:(?P<body>(?:\n[ \t]+.*)+)",
        src,
    )
    assert guard_match is not None and re.search(r"\bmain\(\)", guard_match.group("body")), (
        "the `if __name__ == '__main__':` block must actually CALL main() "
        "(not just define it above the guard, which would exit silently)"
    )
    # 3. The runtime factory module is importable and exposes the
    # canonical production seam.
    runtime_module = importlib.import_module("musubi.vault.runtime")
    assert hasattr(runtime_module, "build_vault_sync_runtime")
    assert hasattr(runtime_module, "VaultSyncRuntime")
    # 4. The repository-owned systemd service file must exist
    # UNCONDITIONALLY (no ``if service_path.exists()`` skip) and must
    # exec the canonical ``python -m musubi.vault.watcher`` module
    # path. A missing or renamed service file would silently let
    # production ship without the live-delete fix wired to the
    # supervisor; this assertion makes that drift visible at PR time.
    service_path = (
        Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "musubi-vault-sync.service"
    )
    assert service_path.exists(), (
        f"repository-owned systemd unit {service_path} must exist; "
        "the live-delete fix is unreachable without it. If the file "
        "was renamed, update this assertion."
    )
    text = service_path.read_text()
    assert "musubi.vault.watcher" in text, (
        f"systemd unit {service_path} must exec `python -m musubi.vault.watcher`; got: {text!r}"
    )


def test_runtime_factory_produces_watcher_construction_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VAULT-003 Blocker 1 (deeper): with a stubbed settings object the
    runtime factory must produce a bundle that successfully
    constructs a ``VaultWatcher`` (i.e. the field names match the
    new required-no-default ``coordinator`` parameter). This
    proves the production wiring is end-to-end constructable, not
    just importable.

    All module-level stubs go through ``monkeypatch`` so the
    ``musubi.vault.runtime`` namespace is restored after the test
    and the suite stays order-independent. The ``bootstrap`` import
    in ``runtime.py`` is patched at the actual seam
    (``musubi.store.bootstrap``), NOT via a dangling
    ``runtime_mod.bootstrap_collections`` attribute that the factory
    never resolves through (the factory does an in-function
    ``from musubi.store import bootstrap as bootstrap_collections``,
    so the symbol's source is ``musubi.store``, not
    ``musubi.vault.runtime``).
    """
    import inspect

    # Build a stub settings object with the minimum fields the
    # factory reads. We use SimpleNamespace to avoid pulling a
    # real Settings (which would require env config + JWT keys).
    # The vault root and lifecycle sqlite path live under pytest's
    # unique ``tmp_path`` so parallel test execution and
    # cross-run flakes cannot collide on a fixed ``/tmp`` path.
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from musubi.vault.runtime import VaultSyncRuntime, build_vault_sync_runtime
    from musubi.vault.watcher import VaultWatcher

    vault_root = tmp_path / "vault_root"
    vault_root.mkdir(parents=True, exist_ok=True)
    lifecycle_sqlite_path = str(tmp_path / "lifecycle.sqlite")
    settings = SimpleNamespace(
        vault_path=str(vault_root),
        qdrant_host="localhost",
        qdrant_port=6333,
        qdrant_api_key=SimpleNamespace(get_secret_value=lambda: "test-key"),
        musubi_allow_plaintext=True,
        tei_dense_url="http://localhost:8080",
        tei_sparse_url="http://localhost:8081",
        tei_reranker_url="http://localhost:8082",
        lifecycle_sqlite_path=lifecycle_sqlite_path,
        lifecycle_pending_cap=1000,
        lifecycle_lease_ttl_s=300.0,
        lifecycle_backoff_base_s=1.0,
        lifecycle_backoff_max_s=60.0,
        lifecycle_sqlite_busy_timeout_ms=5000,
    )
    # The factory calls build_qdrant_client etc. that need real
    # network. Stub each via monkeypatch so the namespace is
    # restored on teardown — the suite stays order-independent.
    # Patch the ACTUAL import seam the factory resolves through:
    # `from musubi.vault.runtime import build_qdrant_client` ->
    # `musubi.vault.runtime.build_qdrant_client`; similarly for
    # bootstrap (resolved via `from musubi.store import bootstrap`
    # inside the factory, but monkeypatch on the attribute the
    # factory binds to is the seam).
    monkeypatch.setattr(
        "musubi.vault.runtime.build_qdrant_client",
        lambda **_kw: MagicMock(name="qdrant"),
    )
    monkeypatch.setattr(
        "musubi.vault.runtime.ChunkedEmbedder",
        lambda _composite: MagicMock(name="embedder"),
    )
    monkeypatch.setattr(
        "musubi.vault.runtime.LifecycleTransitionCoordinator",
        lambda **_kw: MagicMock(name="coordinator"),
    )
    monkeypatch.setattr(
        "musubi.vault.runtime.LifecycleEventSink",
        lambda **_kw: MagicMock(name="sink"),
    )
    monkeypatch.setattr(
        "musubi.vault.runtime.WriteLog",
        lambda **_kw: MagicMock(name="write_log"),
    )
    monkeypatch.setattr(
        "musubi.vault.runtime.CuratedPlane",
        lambda **_kw: MagicMock(name="curated_plane"),
    )
    # bootstrap is imported lazily inside the factory as
    # `from musubi.store import bootstrap as bootstrap_collections`.
    # Patch `musubi.store.bootstrap` (the actual symbol the import
    # binds to) so the factory's call resolves to a no-op.
    monkeypatch.setattr("musubi.store.bootstrap", lambda _qdrant: None)

    runtime: VaultSyncRuntime = build_vault_sync_runtime(settings=settings)  # type: ignore[arg-type]
    assert isinstance(runtime, VaultSyncRuntime)

    # The watcher constructor must accept the runtime's fields as
    # keyword arguments (signature compatibility proof).
    sig = inspect.signature(VaultWatcher.__init__)
    expected_kwargs = {
        "vault_root",
        "curated_plane",
        "write_log",
        "coordinator",
    }
    actual_kwargs = set(sig.parameters.keys())
    assert expected_kwargs.issubset(actual_kwargs), (
        f"VaultWatcher.__init__ must accept {expected_kwargs}; actual: {actual_kwargs}"
    )
    # And `coordinator` must be required (no default) — production-wiring
    # discriminator.
    assert sig.parameters["coordinator"].default is sig.parameters["coordinator"].empty, (
        "coordinator must be a required parameter (no default) — production-wiring discriminator"
    )

    # Construct a real watcher from the runtime fields (MagicMock
    # collaborators are fine — the constructor only stores them).
    watcher = VaultWatcher(
        vault_root=runtime.vault_root,
        curated_plane=runtime.curated_plane,
        write_log=runtime.write_log,
        coordinator=runtime.coordinator,
    )
    assert watcher.coordinator is runtime.coordinator

    # Cleanup the fixture.
    import contextlib

    with contextlib.suppress(OSError):
        vault_root.rmdir()


def test_runtime_factory_wires_curated_plane_with_immutable_publisher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DATA-001 P2 (Yua): the vault runtime is a third write composition — its same-object curated body
    updates publish through the fenced immutable-vector seam. Prove the factory builds the coordinator
    FIRST, registers the dispatcher, and injects the coordinator + a curated-bound publisher into the
    returned CuratedPlane (else the update path fails closed). Only the network deps are stubbed;
    CuratedPlane, the coordinator, and the publisher stay REAL so their wiring is observable."""
    from types import SimpleNamespace

    from qdrant_client import QdrantClient

    from musubi.embedding import FakeEmbedder
    from musubi.store import bootstrap as _bootstrap
    from musubi.store.names import collection_for_plane
    from musubi.vault.runtime import VaultSyncRuntime, build_vault_sync_runtime

    vault_root = tmp_path / "vault_root"
    vault_root.mkdir(parents=True, exist_ok=True)
    settings = SimpleNamespace(
        vault_path=str(vault_root),
        qdrant_host="localhost",
        qdrant_port=6333,
        qdrant_api_key=SimpleNamespace(get_secret_value=lambda: "test-key"),
        musubi_allow_plaintext=True,
        tei_dense_url="http://localhost:8080",
        tei_sparse_url="http://localhost:8081",
        tei_reranker_url="http://localhost:8082",
        lifecycle_sqlite_path=str(tmp_path / "lifecycle.sqlite"),
        lifecycle_pending_cap=1000,
        lifecycle_lease_ttl_s=300.0,
        lifecycle_backoff_base_s=1.0,
        lifecycle_backoff_max_s=60.0,
        lifecycle_sqlite_busy_timeout_ms=5000,
    )
    qc = QdrantClient(":memory:")
    _bootstrap(qc)
    # stub ONLY the network deps; keep CuratedPlane + coordinator + publisher real so wiring is visible.
    monkeypatch.setattr("musubi.vault.runtime.build_qdrant_client", lambda **_kw: qc)
    monkeypatch.setattr("musubi.vault.runtime.ChunkedEmbedder", lambda _composite: FakeEmbedder())
    monkeypatch.setattr("musubi.store.bootstrap", lambda _qdrant: None)

    runtime: VaultSyncRuntime = build_vault_sync_runtime(settings=settings)  # type: ignore[arg-type]
    assert runtime.curated_plane._coordinator is runtime.coordinator, (
        "the curated plane must carry the runtime's coordinator"
    )
    assert runtime.curated_plane._vector_publisher is not None
    assert runtime.curated_plane._vector_publisher._collection == collection_for_plane("curated"), (
        "the curated plane must carry a curated-bound immutable-vector publisher"
    )
    # LOAD-BEARING: the dispatcher must actually be REGISTERED (not merely injected). Drive a REAL
    # same-id curated body update through the returned plane — it fails closed if the runtime stopped
    # calling register_immutable_vector_dispatch.
    import asyncio

    from musubi.store.immutable_vectors import INTENT_KIND
    from musubi.types.curated import CuratedKnowledge

    assert INTENT_KIND in runtime.coordinator._intent_handlers, (
        "the vault runtime must register the immutable-vector dispatcher"
    )
    rt_ns = "eric/vault-runtime/curated"
    first = asyncio.run(
        runtime.curated_plane.create(
            CuratedKnowledge(
                namespace=rt_ns,
                title="T",
                content="body one",
                vault_path="rt.md",
                body_hash="a" * 64,
            )
        )
    )
    updated = asyncio.run(
        runtime.curated_plane.create(
            CuratedKnowledge(
                namespace=rt_ns,
                title="T",
                content="body two is longer",
                vault_path="rt.md",
                body_hash="b" * 64,
                object_id=first.object_id,
            )
        )
    )
    assert updated.content == "body two is longer" and updated.version == first.version + 1, (
        "a same-id body update must commit through the wired dispatcher (not fail closed)"
    )

    import contextlib

    with contextlib.suppress(OSError):
        vault_root.rmdir()


# --------------------------------------------------------------------------- #
# VAULT-003 re-review (Yua 2026-07-15 17:06): behavioral command-path
# discriminator + corrected doc wording.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_find_by_vault_path_uses_limit_two_for_fail_closed(
    plane: CuratedPlane,
    ns: str,
) -> None:
    """VAULT-003 re-review (Yua 17:06) + H12 Copilot round-4: the production
    ``find_by_vault_path`` MUST scroll with ``limit=2`` — the smallest value
    that still surfaces the duplicate case (zero -> not_found, one -> Ok,
    two -> multiple_matches) without pulling an unbounded row count.

    This spies the EXACT scroll request against the real client. The earlier
    ``inspect.getsource`` assertion was false-positive-prone: the method's own
    docstring contains the literal ``limit=2``, so the source check would pass
    even if the actual scroll call regressed."""
    await plane.create(
        _make_curated(
            namespace=ns,
            vault_path="eric/shared/limit-two.md",
            content="Row whose lookup must scroll bounded at limit=2.",
        )
    )

    captured: dict[str, Any] = {}
    real_scroll = plane._client.scroll

    def _spy_scroll(*args: Any, **kwargs: Any) -> Any:
        captured.clear()
        captured.update(kwargs)
        return real_scroll(*args, **kwargs)

    plane._client.scroll = _spy_scroll  # type: ignore[method-assign]
    try:
        result = await plane.find_by_vault_path("eric/shared/limit-two.md")
    finally:
        plane._client.scroll = real_scroll  # type: ignore[method-assign]

    assert isinstance(result, Ok), f"expected Ok resolution, got {result!r}"
    assert captured.get("limit") == 2, (
        "find_by_vault_path must scroll with limit=2: the second match is "
        "sufficient to fail closed, a larger value is wasteful, and limit=1 "
        f"would hide duplicates. captured scroll kwargs={captured!r}"
    )


@pytest.mark.asyncio
async def test_find_by_vault_path_scroll_requests_payload_only(
    plane: CuratedPlane,
    ns: str,
) -> None:
    """H12 Copilot round-3 (Yua ruling 2): ``find_by_vault_path`` rehydrates
    the full payload but never needs the dense + sparse vectors, so it must
    ask Qdrant for ``with_vectors=False`` to avoid shipping the embeddings
    back on every vault delete. This spies the EXACT scroll request against
    the real client rather than reading source text."""
    await plane.create(
        _make_curated(
            namespace=ns,
            vault_path="eric/shared/payload-only.md",
            content="Row whose lookup must not pull vectors back.",
        )
    )

    captured: dict[str, Any] = {}
    real_scroll = plane._client.scroll

    def _spy_scroll(*args: Any, **kwargs: Any) -> Any:
        # Record the LAST scroll's kwargs (find_by_vault_path issues one).
        captured.clear()
        captured.update(kwargs)
        return real_scroll(*args, **kwargs)

    plane._client.scroll = _spy_scroll  # type: ignore[method-assign]
    try:
        result = await plane.find_by_vault_path("eric/shared/payload-only.md")
    finally:
        plane._client.scroll = real_scroll  # type: ignore[method-assign]

    # The lookup resolved (sanity: the spy delegated to the real scroll).
    assert isinstance(result, Ok), f"expected Ok resolution, got {result!r}"
    assert captured.get("with_vectors") is False, (
        "find_by_vault_path must scroll with with_vectors=False (payload-only); "
        f"captured scroll kwargs={captured!r}"
    )


def test_systemd_command_path_reaches_main_behaviorally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VAULT-003 re-review (Yua 17:06): the previous discriminator
    (``test_systemd_module_command_reaches_construction``) was an
    AST+import check, NOT behavioral. The PR body claimed "not
    AST-only" — that wording was misleading. This test provides a
    TRUE behavioral discriminator.

    Strategy: drive the EXACT systemd ``ExecStart=`` command path
    (``python -m musubi.vault.watcher``) via ``runpy.run_module``
    with ``run_name=\"__main__\"``. We monkeypatch
    :func:`asyncio.run` so the module's ``main()`` (which calls
    ``asyncio.run(_main_async())``) is captured as a coroutine that
    we close without ever starting the production runtime. Then we
    assert that the coroutine exists and was created by the module
    — proving the exact command path reaches ``main()`` and
    ``_main_async`` without manual intervention.

    Pre-fix behaviour: the module had no ``__main__`` block and no
    ``main()`` function, so ``runpy.run_module`` would complete
    without invoking anything; ``main_called`` would be ``False``.
    """
    import runpy
    import sys

    captured: dict[str, Any] = {}

    def _fake_asyncio_run(coro: Any, *args: Any, **kwargs: Any) -> Any:
        # Capture the coroutine. Inspect its frame BEFORE closing,
        # because closing the coroutine releases its cr_frame.
        captured["coro"] = coro
        captured["called"] = True
        # Read the source frame to prove `_main_async` was reached.
        cr = coro.cr_frame
        captured["frame_name"] = cr.f_code.co_name if cr is not None else None
        # Close the coroutine to avoid "coroutine was never awaited"
        # warnings.
        import contextlib

        with contextlib.suppress(Exception):
            coro.close()
        return None

    monkeypatch.setattr("musubi.vault.watcher.asyncio.run", _fake_asyncio_run)
    # Also stub the runtime factory so any deeper import path doesn't
    # try to reach a real Qdrant client. The import is inside
    # `_main_async`, so the module attribute is read on the first
    # `from musubi.vault.runtime import build_vault_sync_runtime`.
    # Monkeypatching `sys.modules` lets the late-bound import resolve
    # to our stub without the module needing a top-level reference.
    runtime_stub = type(sys)("musubi.vault.runtime")

    def _stub_factory(**_kw: Any) -> Any:
        raise AssertionError(
            "build_vault_sync_runtime must NOT be invoked by the "
            "behavioural command-path discriminator; main() must "
            "call asyncio.run with the captured coroutine first."
        )

    runtime_stub.build_vault_sync_runtime = _stub_factory  # type: ignore[attr-defined]
    runtime_stub.VaultSyncRuntime = type("VaultSyncRuntime", (), {})  # type: ignore[attr-defined]
    runtime_stub.VaultRuntimeError = type("VaultRuntimeError", (), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "musubi.vault.runtime", runtime_stub)

    # Drive the exact systemd command path: `python -m
    # musubi.vault.watcher`. The pre-fix module had no __main__ block,
    # so this is a no-op; the post-fix module runs main() which calls
    # asyncio.run(_main_async()).
    runpy.run_module("musubi.vault.watcher", run_name="__main__")

    assert captured.get("called"), (
        "the systemd command path did NOT reach asyncio.run; the module "
        "either lacks a __main__ block or the __main__ block does not "
        "call main(). Pre-fix modules fail this assertion."
    )
    # The captured coroutine is the result of `_main_async()`; the
    # function name on the coroutine's frame is the direct evidence
    # that the production entrypoint was reached.
    assert captured.get("frame_name") == "_main_async", (
        f"the captured coroutine must come from _main_async (the "
        f"production entrypoint); got {captured.get('frame_name')!r}. "
        f"Pre-fix modules never reach this point because they have no "
        f"main() and no __main__ block."
    )


# --------------------------------------------------------------------------- #
# VAULT-003 round-5 (Yua 17:50): runtime module is self-contained
# --------------------------------------------------------------------------- #


def test_runtime_module_does_not_import_watcher() -> None:
    """VAULT-003 round-5: the watcher entrypoint module
    (``musubi.vault.watcher``) is executed via
    ``python -m musubi.vault.watcher``. To prevent the
    ``__main__`` re-entry bug (Python executes ``python -m
    musubi.vault.watcher`` under ``musubi.vault.watcher.__main__``,
    and a qualified ``import musubi.vault.watcher`` would return the
    ``__main__`` module — not the regular module — creating
    duplicate module state), the runtime module
    (``musubi.vault.runtime``) MUST NOT import
    ``musubi.vault.watcher`` at any point.

    This test runs a TRUE COLD-START subprocess (Python ``-c``) so
    other tests that have already imported ``musubi.vault.watcher``
    (e.g. ``test_systemd_command_path_reaches_main_behaviorally``
    uses ``runpy.run_module`` which leaves the module in
    ``sys.modules``) cannot pollute the constraint. The subprocess
    prints the sorted list of ``musubi.vault.*`` modules present
    in ``sys.modules`` AFTER ``import musubi.vault.runtime``; we
    assert ``musubi.vault.watcher`` is NOT in that list.
    """
    import json
    import subprocess
    import sys
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[2]
    script = (
        # pytest's ``pythonpath = ["src"]`` does NOT propagate into a
        # ``python -c`` subprocess, so put the src-layout root on
        # sys.path explicitly — otherwise this cold-start import is
        # brittle in any environment where musubi isn't installed
        # editable (Copilot review, PR #562).
        f"import sys; sys.path.insert(0, {str(repo_root / 'src')!r}); "
        "import json; "
        "import musubi.vault.runtime; "
        "mods = sorted(k for k in sys.modules if k.startswith('musubi.vault')); "
        "sys.stdout.write(json.dumps(mods) + chr(10))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=repo_root,
        # Bound the child so an import deadlock / environment hang fails fast
        # instead of stalling CI. 30s matches the nearby test_c6_event_loss.py
        # precedent and is ample for a cold-start import that normally completes
        # in a few seconds (Copilot PR #562).
        timeout=30,
    )
    assert result.returncode == 0, f"cold-start subprocess failed: stderr={result.stderr!r}"
    mods = json.loads(result.stdout.strip())
    assert "musubi.vault.runtime" in mods, (
        f"sanity: runtime module must be in sys.modules after import; got: {mods!r}"
    )
    assert "musubi.vault.watcher" not in mods, (
        f"importing musubi.vault.runtime in a cold-start subprocess "
        f"must NOT pull musubi.vault.watcher into sys.modules (the "
        f"runtime factory would otherwise re-load the entrypoint "
        f"module under __main__ when invoked from `python -m "
        f"musubi.vault.watcher`). sys.modules musubi.vault.* = {mods!r}"
    )


def test_runtime_factory_does_not_import_watcher(tmp_path: Path) -> None:
    """VAULT-003 round-5: same constraint as above, but verified
    during the FACTORY CALL. The factory's import graph stays
    inside ``musubi.vault.runtime`` — no transitive
    ``import musubi.vault.watcher``. We construct the factory
    inside a TRUE COLD-START subprocess (so other tests that have
    already imported ``musubi.vault.watcher`` via ``runpy.run_module``
    cannot pollute the constraint) and re-assert the constraint
    after the call. The factory needs a real vault root on disk
    and a writable sqlite path; both come from pytest's
    ``tmp_path`` so the test is order-independent and free of
    cross-run ``/tmp`` collisions.
    """
    import json
    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    vault_root = str(tmp_path / "vault_root")
    lifecycle_sqlite_path = str(tmp_path / "lifecycle.sqlite")
    # Create the vault root inside the host so the subprocess can
    # see it (the subprocess inherits cwd, not the fixture).
    import os

    os.makedirs(vault_root, exist_ok=True)
    # Stub the factory collaborators inside the subprocess so the
    # call doesn't need a real Qdrant / TEI / coordinator. We embed
    # the stubs as a Python heredoc.
    script = (
        # pytest's ``pythonpath = ["src"]`` does NOT propagate into a
        # ``python -c`` subprocess; put the src-layout root on sys.path
        # explicitly so this cold-start factory call imports musubi
        # even when the package isn't installed editable (Copilot
        # review, PR #562).
        f"import sys; sys.path.insert(0, {str(repo_root / 'src')!r}); "
        "import json; "
        "from types import SimpleNamespace; "
        "from unittest.mock import MagicMock; "
        "import musubi.vault.runtime as rt; "
        "rt.build_qdrant_client = lambda **_kw: MagicMock(name='qdrant'); "
        "rt.ChunkedEmbedder = lambda _c: MagicMock(name='embedder'); "
        "rt.LifecycleTransitionCoordinator = lambda **_kw: MagicMock(name='coord'); "
        "rt.LifecycleEventSink = lambda **_kw: MagicMock(name='sink'); "
        "rt.WriteLog = lambda **_kw: MagicMock(name='wlog'); "
        "rt.CuratedPlane = lambda **_kw: MagicMock(name='cplane'); "
        "import musubi.store as _store; "
        "_store.bootstrap = lambda _q: None; "
        "settings = SimpleNamespace("
        f"  vault_path={vault_root!r}, "
        "  qdrant_host='localhost', qdrant_port=6333, "
        "  qdrant_api_key=SimpleNamespace(get_secret_value=lambda: 'k'), "
        "  musubi_allow_plaintext=True, "
        "  tei_dense_url='http://a', tei_sparse_url='http://b', tei_reranker_url='http://c', "
        f"  lifecycle_sqlite_path={lifecycle_sqlite_path!r}, "
        "  lifecycle_pending_cap=1, lifecycle_lease_ttl_s=1.0, "
        "  lifecycle_backoff_base_s=1.0, lifecycle_backoff_max_s=1.0, "
        "  lifecycle_sqlite_busy_timeout_ms=1, "
        "); "
        "rt.build_vault_sync_runtime(settings=settings); "
        "mods = sorted(k for k in sys.modules if k.startswith('musubi.vault')); "
        "sys.stdout.write(json.dumps(mods) + chr(10))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=repo_root,
        # Bound the child so an import deadlock / environment hang fails fast
        # instead of stalling CI. 30s matches the nearby test_c6_event_loss.py
        # precedent and is ample for a cold-start import that normally completes
        # in a few seconds (Copilot PR #562).
        timeout=30,
    )
    assert result.returncode == 0, f"cold-start subprocess failed: stderr={result.stderr!r}"
    mods = json.loads(result.stdout.strip())
    assert "musubi.vault.watcher" not in mods, (
        f"calling build_vault_sync_runtime in a cold-start "
        f"subprocess must NOT pull musubi.vault.watcher into "
        f"sys.modules (would re-load the entrypoint module under "
        f"__main__ when invoked from `python -m "
        f"musubi.vault.watcher`). sys.modules musubi.vault.* = "
        f"{mods!r}"
    )


# --------------------------------------------------------------------------- #
# VAULT-003 round-6 (Yua 18:11): file-path refusal + tmp_path + unconditional
# systemd proof.
# --------------------------------------------------------------------------- #


def test_runtime_rejects_vault_path_that_is_a_regular_file(
    tmp_path: Path,
) -> None:
    """VAULT-003 round-6: ``build_vault_sync_runtime`` must refuse
    to start when ``settings.vault_path`` is a regular file (not
    a directory). The watcher must fail closed + visibly rather
    than silently booting against a dangling/non-directory path.
    This test makes the narrowed docstring truthful (the
    round-5 docstring says ``VaultRuntimeError`` is raised for
    missing OR invalid ``vault_path``; this test pins the
    regular-file refusal case)."""
    from types import SimpleNamespace

    import pytest

    from musubi.vault.runtime import VaultRuntimeError, build_vault_sync_runtime

    # Create a real file (not a directory) under pytest's unique
    # tmp_path; that fixture guarantees cross-run isolation.
    file_path = tmp_path / "not_a_vault.md"
    file_path.write_text("# not a vault root\n", encoding="utf-8")
    assert file_path.exists() and file_path.is_file()

    settings = SimpleNamespace(
        vault_path=str(file_path),
        # The factory reads these but raises BEFORE touching them
        # on the file-path refusal path; values are placeholders.
        qdrant_host="x",
        qdrant_port=0,
        qdrant_api_key=SimpleNamespace(get_secret_value=lambda: "x"),
        musubi_allow_plaintext=True,
        tei_dense_url="x",
        tei_sparse_url="x",
        tei_reranker_url="x",
        lifecycle_sqlite_path=str(tmp_path / "lifecycle.sqlite"),
        lifecycle_pending_cap=1,
        lifecycle_lease_ttl_s=1.0,
        lifecycle_backoff_base_s=1.0,
        lifecycle_backoff_max_s=1.0,
        lifecycle_sqlite_busy_timeout_ms=1,
    )
    with pytest.raises(VaultRuntimeError) as excinfo:
        build_vault_sync_runtime(settings=settings)  # type: ignore[arg-type]
    msg = str(excinfo.value)
    assert str(file_path) in msg, (
        f"VaultRuntimeError message must include the offending "
        f"vault_path for operator actionability; got: {msg!r}"
    )
    assert "not a directory" in msg, (
        f"VaultRuntimeError message must say 'not a directory' so "
        f"the cause is obvious in the systemd journal; got: {msg!r}"
    )
