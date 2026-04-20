from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / ".operator" / "scripts"))
from board import build_board_data  # type: ignore


@patch("board.get_open_prs")
@patch("board.load_slices")
@patch("board.load_issues")
@patch("board.get_open_issues")
@patch("board.get_branch_age_hours")
@patch("pathlib.Path.exists")
def test_board_lists_all_open_prs(
    mock_exists: Any,
    mock_branch_age: Any,
    mock_open_issues: Any,
    mock_load_issues: Any,
    mock_load_slices: Any,
    mock_get_prs: Any,
) -> None:
    """test_board_lists_all_open_prs"""
    mock_get_prs.return_value = [
        {
            "number": 1,
            "title": "PR 1",
            "author": {"login": "eric"},
            "isDraft": False,
            "mergeStateStatus": "CLEAN",
        }
    ]
    mock_load_slices.return_value = {}
    mock_load_issues.return_value = {}
    mock_open_issues.return_value = []

    data = build_board_data()
    assert len(data["prs"]) == 1
    assert data["prs"][0]["number"] == 1


@patch("board.get_open_prs")
@patch("board.load_slices")
@patch("board.load_issues")
@patch("board.get_open_issues")
@patch("board.get_branch_age_hours")
@patch("pathlib.Path.exists")
def test_board_separates_ready_and_draft_prs(
    mock_exists: Any,
    mock_branch_age: Any,
    mock_open_issues: Any,
    mock_load_issues: Any,
    mock_load_slices: Any,
    mock_get_prs: Any,
) -> None:
    """test_board_separates_ready_and_draft_prs"""
    mock_get_prs.return_value = [
        {"number": 1, "title": "PR 1", "isDraft": False, "mergeStateStatus": "CLEAN"},
        {"number": 2, "title": "PR 2", "isDraft": True, "mergeStateStatus": "UNKNOWN"},
    ]
    mock_load_slices.return_value = {}
    mock_load_issues.return_value = {}
    mock_open_issues.return_value = []

    data = build_board_data()
    assert len(data["prs"]) == 2
    assert data["prs"][0]["isDraft"] is False
    assert data["prs"][1]["isDraft"] is True


@patch("board.get_open_prs")
@patch("board.load_slices")
@patch("board.load_issues")
@patch("board.get_open_issues")
@patch("board.get_branch_age_hours")
@patch("pathlib.Path.exists")
def test_board_flags_ci_failing_prs(
    mock_exists: Any,
    mock_branch_age: Any,
    mock_open_issues: Any,
    mock_load_issues: Any,
    mock_load_slices: Any,
    mock_get_prs: Any,
) -> None:
    """test_board_flags_ci_failing_prs"""
    mock_get_prs.return_value = [
        {
            "number": 1,
            "title": "PR 1",
            "isDraft": False,
            "mergeStateStatus": "UNSTABLE",
            "statusCheckRollup": [],
        }
    ]
    mock_load_slices.return_value = {}
    mock_load_issues.return_value = {}
    mock_open_issues.return_value = []

    data = build_board_data()
    assert data["prs"][0]["mergeStateStatus"] == "UNSTABLE"


@patch("board.get_open_prs")
@patch("board.load_slices")
@patch("board.load_issues")
@patch("board.get_open_issues")
@patch("board.get_branch_age_hours")
@patch("pathlib.Path.exists")
def test_board_counts_slices_by_status(
    mock_exists: Any,
    mock_branch_age: Any,
    mock_open_issues: Any,
    mock_load_issues: Any,
    mock_load_slices: Any,
    mock_get_prs: Any,
) -> None:
    """test_board_counts_slices_by_status"""

    class MockSlice:
        def __init__(self, status: str) -> None:
            self.status = status
            self.depends_on: list[str] = []

    mock_load_slices.return_value = {
        "s1": MockSlice("done"),
        "s2": MockSlice("done"),
        "s3": MockSlice("ready"),
    }
    mock_load_issues.return_value = {}
    mock_open_issues.return_value = []
    mock_get_prs.return_value = []

    data = build_board_data()
    sc = data["status_counts"]
    assert sc["done"] == 2
    assert sc["ready"] == 1


@patch("board.get_open_prs")
@patch("board.load_slices")
@patch("board.load_issues")
@patch("board.get_open_issues")
@patch("board.get_branch_age_hours")
@patch("pathlib.Path.exists")
@patch("pathlib.Path.read_text")
@patch("pathlib.Path.stat")
@patch("time.time")
def test_board_detects_in_flight_via_lock_files(
    mock_time: Any,
    mock_stat: Any,
    mock_read_text: Any,
    mock_exists: Any,
    mock_branch_age: Any,
    mock_open_issues: Any,
    mock_load_issues: Any,
    mock_load_slices: Any,
    mock_get_prs: Any,
) -> None:
    """test_board_detects_in_flight_via_lock_files"""

    class MockSlice:
        def __init__(self, status: str) -> None:
            self.status = status
            self.depends_on: list[str] = []

    mock_load_slices.return_value = {
        "s1": MockSlice("in-progress"),
    }
    mock_load_issues.return_value = {}
    mock_open_issues.return_value = []
    mock_get_prs.return_value = []
    mock_exists.return_value = True
    mock_read_text.return_value = "agent1  2026-04-19"

    stat_mock = MagicMock()
    stat_mock.st_mtime = 1000
    mock_stat.return_value = stat_mock
    mock_time.return_value = 1000 + 3600 * 2  # 2 hours old

    data = build_board_data()
    assert len(data["in_flight"]) == 1
    assert data["in_flight"][0]["id"] == "s1"
    assert data["in_flight"][0]["agent"] == "agent1"


@patch("board.get_open_prs")
@patch("board.load_slices")
@patch("board.load_issues")
@patch("board.get_open_issues")
@patch("board.get_branch_age_hours")
@patch("pathlib.Path.exists")
def test_board_detects_in_review_limbo_via_branch_age(
    mock_exists: Any,
    mock_branch_age: Any,
    mock_open_issues: Any,
    mock_load_issues: Any,
    mock_load_slices: Any,
    mock_get_prs: Any,
) -> None:
    """test_board_detects_in_review_limbo_via_branch_age"""

    class MockSlice:
        def __init__(self, status: str) -> None:
            self.status = status
            self.depends_on: list[str] = []

    mock_load_slices.return_value = {
        "s1": MockSlice("in-review"),
    }
    mock_load_issues.return_value = {}
    mock_open_issues.return_value = []
    mock_get_prs.return_value = []
    mock_branch_age.return_value = 5.0  # 5 hours old

    data = build_board_data()
    assert len(data["in_review_limbo"]) == 1
    assert data["in_review_limbo"][0]["id"] == "s1"


@patch("board.get_open_prs")
@patch("board.load_slices")
@patch("board.load_issues")
@patch("board.get_open_issues")
@patch("board.get_branch_age_hours")
@patch("pathlib.Path.exists")
def test_board_json_mode_produces_valid_json(
    mock_exists: Any,
    mock_branch_age: Any,
    mock_open_issues: Any,
    mock_load_issues: Any,
    mock_load_slices: Any,
    mock_get_prs: Any,
) -> None:
    """test_board_json_mode_produces_valid_json"""
    mock_load_slices.return_value = {}
    mock_load_issues.return_value = {}
    mock_open_issues.return_value = []
    mock_get_prs.return_value = []

    data = build_board_data()
    out = json.dumps(data)
    assert isinstance(out, str)


@patch("board.get_open_prs")
@patch("board.load_slices")
@patch("board.load_issues")
@patch("board.get_open_issues")
@patch("board.get_branch_age_hours")
@patch("pathlib.Path.exists")
def test_board_empty_board_renders_cleanly(
    mock_exists: Any,
    mock_branch_age: Any,
    mock_open_issues: Any,
    mock_load_issues: Any,
    mock_load_slices: Any,
    mock_get_prs: Any,
) -> None:
    """test_board_empty_board_renders_cleanly"""
    mock_load_slices.return_value = {}
    mock_load_issues.return_value = {}
    mock_open_issues.return_value = []
    mock_get_prs.return_value = []

    data = build_board_data()
    assert len(data["prs"]) == 0
    assert len(data["ready"]) == 0
