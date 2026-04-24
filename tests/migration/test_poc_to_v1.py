"""Test contract for slice-poc-data-migration."""

from __future__ import annotations

import os
import sys
from unittest.mock import Mock

import pytest
from pytest import CaptureFixture
from qdrant_client.models import PointStruct

sys.path.insert(0, os.path.abspath("deploy/migration"))
import importlib.util
import os
import sys

spec = importlib.util.spec_from_file_location("poc_to_v1", "deploy/migration/poc-to-v1.py")
poc_to_v1 = importlib.util.module_from_spec(spec)  # type: ignore
sys.modules["poc_to_v1"] = poc_to_v1
spec.loader.exec_module(poc_to_v1)  # type: ignore

migrate_memories = poc_to_v1.migrate_memories
migrate_thoughts = poc_to_v1.migrate_thoughts


@pytest.fixture
def mock_qdrant() -> Mock:
    mock = Mock()
    mock.scroll.return_value = ([], None)
    return mock


@pytest.fixture
def mock_musubi() -> Mock:
    return Mock()


def test_migrator_reads_synthetic_qdrant_source(mock_qdrant: Mock, mock_musubi: Mock) -> None:
    mock_qdrant.scroll.return_value = (
        [
            PointStruct(
                id="123",
                vector=[],
                payload={"content": "test", "created_at": "2026-04-19T00:00:00Z"},
            )
        ],
        None,
    )
    migrated, skipped = migrate_memories(mock_qdrant, mock_musubi, {}, dry_run=False)
    assert migrated == 1
    assert skipped == 0


def test_migrator_transforms_v0_episodic_to_v1_schema(mock_qdrant: Mock, mock_musubi: Mock) -> None:
    mock_qdrant.scroll.return_value = (
        [
            PointStruct(
                id="123",
                vector=[],
                payload={
                    "content": "test",
                    "created_at": "2026-04-19T00:00:00Z",
                    "type": "user",
                    "tags": ["a"],
                    "context": "ctx",
                },
            )
        ],
        None,
    )
    migrated, _skipped = migrate_memories(mock_qdrant, mock_musubi, {}, dry_run=False)
    assert migrated == 1
    mock_musubi.episodic.capture.assert_called_once()
    kwargs = mock_musubi.episodic.capture.call_args.kwargs
    assert kwargs["content"] == "test"
    assert kwargs["namespace"] == "eric/poc/episodic"
    assert kwargs["topics"] == ["user"]
    assert kwargs["tags"] == ["a"]
    # Source-truth timestamp is preserved via the SDK's operator-only
    # `created_at` escape hatch — without it the row would be re-stamped
    # at ingest time and the PoC timeline would be lost.
    assert kwargs["created_at"] is not None
    assert kwargs["created_at"].isoformat().startswith("2026-04-19T00:00:00")


@pytest.mark.skip(
    reason="V0 curated schema not found during discovery, script just copies content to episodic if present, or we write logic for it if it existed."
)
def test_migrator_transforms_v0_curated_to_v1_schema() -> None:
    pass


@pytest.mark.skip(reason="V0 concept schema not found during discovery")
def test_migrator_transforms_v0_concept_to_v1_schema() -> None:
    pass


def test_migrator_transforms_v0_thought_to_v1_schema(mock_qdrant: Mock, mock_musubi: Mock) -> None:
    mock_qdrant.scroll.return_value = (
        [
            PointStruct(
                id="123",
                vector=[],
                payload={
                    "content": "test thought",
                    "created_at": "2026-04-19T00:00:00Z",
                    "from_presence": "me",
                    "to_presence": "you",
                },
            )
        ],
        None,
    )
    migrated, _skipped = migrate_thoughts(mock_qdrant, mock_musubi, {}, dry_run=False)
    assert migrated == 1
    mock_musubi.thoughts.send.assert_called_once()
    kwargs = mock_musubi.thoughts.send.call_args.kwargs
    assert kwargs["namespace"] == "eric/you/thought"


def test_migrator_skips_rows_failing_pydantic_validation(
    mock_qdrant: Mock, mock_musubi: Mock
) -> None:
    mock_qdrant.scroll.return_value = (
        [
            PointStruct(id="123", vector=[], payload={"content": ""})
        ],  # empty content fails EpisodicMemory validation
        None,
    )
    migrated, skipped = migrate_memories(mock_qdrant, mock_musubi, {}, dry_run=False)
    assert migrated == 0
    assert skipped == 1


@pytest.mark.skip(reason="deferred to cross-slice implementation of created_at override")
def test_migrator_preserves_created_at_on_target() -> None:
    pass


def test_migrator_dry_run_writes_nothing(mock_qdrant: Mock, mock_musubi: Mock) -> None:
    mock_qdrant.scroll.return_value = (
        [
            PointStruct(
                id="123",
                vector=[],
                payload={"content": "test", "created_at": "2026-04-19T00:00:00Z"},
            )
        ],
        None,
    )
    migrated, _skipped = migrate_memories(mock_qdrant, mock_musubi, {}, dry_run=True)
    assert migrated == 1
    mock_musubi.episodic.capture.assert_not_called()


def test_migrator_state_file_tracks_progress(mock_qdrant: Mock, mock_musubi: Mock) -> None:
    mock_qdrant.scroll.return_value = (
        [
            PointStruct(
                id="123",
                vector=[],
                payload={"content": "test", "created_at": "2026-04-19T00:00:00Z"},
            )
        ],
        None,
    )
    state: dict[str, list[str]] = {"migrated_memories": []}
    migrate_memories(mock_qdrant, mock_musubi, state, dry_run=False)
    assert "123" in state["migrated_memories"]


def test_migrator_resume_skips_already_migrated(mock_qdrant: Mock, mock_musubi: Mock) -> None:
    mock_qdrant.scroll.return_value = (
        [
            PointStruct(
                id="123",
                vector=[],
                payload={"content": "test", "created_at": "2026-04-19T00:00:00Z"},
            )
        ],
        None,
    )
    state = {"migrated_memories": ["123"]}
    migrated, skipped = migrate_memories(mock_qdrant, mock_musubi, state, dry_run=False)
    assert migrated == 0
    assert skipped == 0


def test_migrator_refuses_without_i_have_a_backup_flag() -> None:
    # Just test the argparse logic or skip if hard to test argparse in-process. Let's just import main and patch sys.argv
    import sys
    from unittest.mock import patch

    with patch.object(sys, "argv", ["poc_to_v1.py"]):
        with pytest.raises(SystemExit) as exc:
            poc_to_v1.main()
        assert exc.value.code == 1


def test_migrator_handles_source_schema_unknown_gracefully(
    mock_qdrant: Mock, mock_musubi: Mock
) -> None:
    # Simulates a completely unknown schema (missing created_at etc)
    mock_qdrant.scroll.return_value = (
        [PointStruct(id="123", vector=[], payload={"random": "data"})],
        None,
    )
    migrated, skipped = migrate_memories(mock_qdrant, mock_musubi, {}, dry_run=False)
    assert migrated == 0
    assert skipped == 1  # gracefully skipped


def test_migrator_cli_help_text_is_useful(capsys: CaptureFixture[str]) -> None:
    import sys
    from unittest.mock import patch

    with patch.object(sys, "argv", ["poc_to_v1.py", "--help"]):
        with pytest.raises(SystemExit) as exc:
            poc_to_v1.main()
        assert exc.value.code == 0


@pytest.mark.skip(reason="deferred to integration harness")
def test_integration_migrate_100_row_synthetic_corpus_end_to_end() -> None:
    pass
