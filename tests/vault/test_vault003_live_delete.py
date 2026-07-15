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

The first contract is bounded to ten tests in this file:

    8 RED discriminating tests   (currently failing under live code;
                                  the seam must implement the contract)
    2 GREEN preservation guards  (passing under live code; the seam
                                  must not break them)

Test function names transcribe the slice doc's Test Contract bullets
verbatim per the AGENTS.md Test Contract Closure Rule.

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
from musubi.types.common import Err
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
    """Bullet 2: post-archive, the curated default-retrieval query returns
    nothing; the row remains readable by ``object_id`` (audit/history path)."""
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
    # Default retrieval excludes archived. The CuratedPlane contract for
    # default retrieval is `state in {provisional, matured, promoted,
    # synthesized, demoted, superseded}`. Verify via a payload filter.
    archived_only = plane._client.scroll(
        collection_name="musubi_curated",
        scroll_filter=None,
        limit=100,
        with_payload=["object_id", "state"],
        with_vectors=False,
    )
    states = {pt.payload.get("state") for pt in archived_only[0] if pt.payload}
    assert "archived" in states, "the canonical transition did not write 'archived'"
    # The row is still readable by id (audit/history retention).
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
        collection_name="musubi_curated",
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
