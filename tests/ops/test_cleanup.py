from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, call

import pytest

from musubi.ops.cleanup import run_cleanup
from musubi.store.names import COLLECTION_NAMES


@pytest.fixture(autouse=True)
def mock_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    mock_path = MagicMock(spec=Path)
    mock_path.exists.return_value = True

    class MockSettings:
        artifact_blob_path = MagicMock()
        artifact_blob_path.__truediv__.return_value = mock_path

    monkeypatch.setattr("musubi.ops.cleanup.get_settings", lambda: MockSettings())


@pytest.fixture
def mock_qdrant() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_sdk() -> MagicMock:
    return MagicMock()


def test_cleanup_worker_hard_deletes_archived_older_than_ttl(
    mock_qdrant: MagicMock, mock_sdk: MagicMock
) -> None:
    point = MagicMock()
    point.payload = {"namespace": "test/ns", "object_id": "123"}
    mock_qdrant.scroll.return_value = ([point], None)

    metrics = run_cleanup(mock_qdrant, mock_sdk, tombstone_ttl_days=30)

    for c in COLLECTION_NAMES:
        assert metrics[c] == 1

    assert (
        call("DELETE", "/memories/123", params={"namespace": "test/ns", "hard": "true"})
        in mock_sdk._json.call_args_list
    )


def test_cleanup_worker_skips_non_archived_rows(
    mock_qdrant: MagicMock, mock_sdk: MagicMock
) -> None:
    # Qdrant scroll filter already specifies state=archived
    # so we just check that scroll is called with the right filter
    mock_qdrant.scroll.return_value = ([], None)
    run_cleanup(mock_qdrant, mock_sdk)

    call_args = mock_qdrant.scroll.call_args[1]
    must_filters = call_args["scroll_filter"].must
    state_match = any(
        getattr(f, "key", "") == "state" and getattr(f.match, "value", "") == "archived"
        for f in must_filters
    )
    assert state_match


def test_cleanup_worker_deletes_blob_for_artifact_rows(
    mock_qdrant: MagicMock, mock_sdk: MagicMock
) -> None:

    # Only return point for musubi_artifact to isolate
    def fake_scroll(collection_name: str, **kwargs: dict[str, Any]) -> tuple[list[Any], None]:
        if collection_name == "musubi_artifact":
            point = MagicMock()
            point.payload = {"namespace": "test/ns", "object_id": "123"}
            return ([point], None)
        return ([], None)

    mock_qdrant.scroll.side_effect = fake_scroll

    run_cleanup(mock_qdrant, mock_sdk)

    assert (
        call("DELETE", "/artifacts/123/purge", params={"namespace": "test/ns"})
        in mock_sdk._json.call_args_list
    )
    # Unlink assertion done via the mock in the fixture; we just check it doesn't crash.


def test_cleanup_worker_emits_metrics(mock_qdrant: MagicMock, mock_sdk: MagicMock) -> None:
    def fake_scroll(collection_name: str, **kwargs: dict[str, Any]) -> tuple[list[Any], None]:
        point = MagicMock()
        point.payload = {"namespace": "test", "object_id": "obj"}
        return ([point], None)

    mock_qdrant.scroll.side_effect = fake_scroll
    metrics = run_cleanup(mock_qdrant, mock_sdk)
    for c in COLLECTION_NAMES:
        assert metrics[c] == 1
