from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from musubi.ops.retention import run_retention


@pytest.fixture
def mock_qdrant() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_sdk() -> MagicMock:
    return MagicMock()


def test_retention_worker_respects_per_plane_config(
    mock_qdrant: MagicMock, mock_sdk: MagicMock
) -> None:
    point = MagicMock()
    point.payload = {"namespace": "test/ns", "object_id": "123"}
    mock_qdrant.scroll.return_value = ([point], None)

    metrics = run_retention(mock_qdrant, mock_sdk, policies={"episodic": 10})
    assert "musubi_episodic" in metrics
    assert metrics["musubi_episodic"] == 1
    assert "musubi_thought" not in metrics


def test_retention_worker_thoughts_default_30d(mock_qdrant: MagicMock, mock_sdk: MagicMock) -> None:
    mock_qdrant.scroll.return_value = ([], None)
    run_retention(mock_qdrant, mock_sdk)

    call_args = mock_qdrant.scroll.call_args[1]
    assert call_args["collection_name"] == "musubi_thought"


def test_retention_worker_episodic_default_unlimited(
    mock_qdrant: MagicMock, mock_sdk: MagicMock
) -> None:
    mock_qdrant.scroll.return_value = ([], None)
    metrics = run_retention(mock_qdrant, mock_sdk)

    # episodic is unlimited so it should not be in the metrics
    assert "musubi_episodic" not in metrics


def test_retention_refuses_stored_unindexed_artifact_policy_candidate(
    mock_qdrant: MagicMock, mock_sdk: MagicMock
) -> None:
    escrow = MagicMock()
    escrow.payload = {
        "namespace": "eric/command-chair/artifact",
        "object_id": "3H1q1qtb3BGwodFekT1omv2IVZZ",
        "artifact_state": "stored_unindexed",
    }
    ordinary = MagicMock()
    ordinary.payload = {
        "namespace": "eric/command-chair/artifact",
        "object_id": "3H1q1vkf5A6gJlMlUyyghHj65i0",
        "artifact_state": "indexed",
    }
    mock_qdrant.scroll.return_value = ([escrow, ordinary], None)

    metrics = run_retention(mock_qdrant, mock_sdk, policies={"artifact": 1})

    assert metrics == {"musubi_artifact": 1}
    mock_sdk._json.assert_called_once_with(
        "DELETE",
        "/artifacts/3H1q1vkf5A6gJlMlUyyghHj65i0/purge",
        params={"namespace": "eric/command-chair/artifact"},
    )
