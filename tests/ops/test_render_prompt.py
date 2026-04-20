from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / ".operator" / "scripts"))

import importlib.util

spec = importlib.util.spec_from_file_location(
    "render_prompt",
    str(
        Path(__file__).resolve().parent.parent.parent / ".operator" / "scripts" / "render-prompt.py"
    ),
)
if spec and spec.loader:
    render_prompt_mod = importlib.util.module_from_spec(spec)
    sys.modules["render_prompt"] = render_prompt_mod
    spec.loader.exec_module(render_prompt_mod)
    from claimable import REPO_ROOT  # type: ignore
    from render_prompt import render_prompt  # type: ignore


class MockSlice:
    def __init__(
        self,
        status: str,
        specs: list[str],
        owns_paths: list[str],
        forbidden_paths: list[str],
        depends_on: list[str],
        path: Path,
    ) -> None:
        self.status = status
        self.specs = specs
        self.owns_paths = owns_paths
        self.forbidden_paths = forbidden_paths
        self.depends_on = depends_on
        self.path = path


class MockIssue:
    def __init__(self, number: int) -> None:
        self.number = number


@patch("render_prompt.load_slices")
@patch("render_prompt.load_issues")
@patch("render_prompt.get_parallel_agents")
@patch("sys.stdout", new_callable=MagicMock)
def test_render_start_slice_produces_expected_shape(
    mock_stdout: Any, mock_pa: Any, mock_li: Any, mock_ls: Any
) -> None:
    mock_ls.return_value = {
        "slice-foo": MockSlice(
            "ready",
            ["spec.md"],
            ["src/foo.py"],
            ["src/bar.py"],
            ["slice-dep"],
            REPO_ROOT / "docs/architecture/_slices/slice-foo.md",
        ),
        "slice-dep": MockSlice("done", [], [], [], [], Path("")),
    }
    mock_li.return_value = {"slice-foo": MockIssue(42)}
    mock_pa.return_value = []

    render_prompt("gemini", "slice-foo", "~/clone", "slice-start", False, False)

    out = mock_stdout.write.call_args_list
    full_out = "".join(call[0][0] for call in out)
    assert "You are taking on slice-foo (Issue #42)." in full_out
    assert "────── BRIEF (gemini → slice-foo) ──────" in full_out
    assert "- src/foo.py" in full_out
    assert "- src/bar.py" in full_out
    assert "- slice-dep (done)" in full_out


@patch("render_prompt.load_slices")
@patch("render_prompt.load_issues")
def test_render_errors_cleanly_on_missing_issue(mock_li: Any, mock_ls: Any) -> None:
    mock_ls.return_value = {
        "slice-foo": MockSlice("ready", ["spec.md"], ["src/foo.py"], [], [], Path(""))
    }
    mock_li.return_value = {}  # missing issue
    with pytest.raises(SystemExit):
        render_prompt("gemini", "slice-foo", "~/clone", "slice-start", False, False)


@patch("render_prompt.load_slices")
def test_render_errors_cleanly_on_missing_slice_id(mock_ls: Any) -> None:
    mock_ls.return_value = {}
    with pytest.raises(SystemExit):
        render_prompt("gemini", "slice-missing", "~/clone", "slice-start", False, False)


@patch("render_prompt.load_slices")
@patch("render_prompt.load_issues")
@patch("render_prompt.get_parallel_agents")
@patch("sys.stdout", new_callable=MagicMock)
def test_render_includes_parallel_agents_section_when_any_in_progress(
    mock_stdout: Any, mock_pa: Any, mock_li: Any, mock_ls: Any
) -> None:
    mock_ls.return_value = {
        "slice-foo": MockSlice(
            "ready",
            ["spec.md"],
            ["src/foo.py"],
            [],
            [],
            REPO_ROOT / "docs/architecture/_slices/slice-foo.md",
        )
    }
    mock_li.return_value = {"slice-foo": MockIssue(42)}
    mock_pa.return_value = [{"slice_id": "slice-other", "agent": "codex", "age_h": 1.5}]

    render_prompt("gemini", "slice-foo", "~/clone", "slice-start", False, False)

    out = "".join(call[0][0] for call in mock_stdout.write.call_args_list)
    assert "Parallel agents active right now" in out
    assert "- codex on slice-other" in out


@patch("render_prompt.load_slices")
@patch("render_prompt.load_issues")
@patch("render_prompt.get_parallel_agents")
@patch("sys.stdout", new_callable=MagicMock)
def test_render_omits_parallel_agents_section_when_none(
    mock_stdout: Any, mock_pa: Any, mock_li: Any, mock_ls: Any
) -> None:
    mock_ls.return_value = {
        "slice-foo": MockSlice(
            "ready",
            ["spec.md"],
            ["src/foo.py"],
            [],
            [],
            REPO_ROOT / "docs/architecture/_slices/slice-foo.md",
        )
    }
    mock_li.return_value = {"slice-foo": MockIssue(42)}
    mock_pa.return_value = []

    render_prompt("gemini", "slice-foo", "~/clone", "slice-start", False, False)

    out = "".join(call[0][0] for call in mock_stdout.write.call_args_list)
    assert "Parallel agents active right now" not in out


@patch("render_prompt.load_slices")
@patch("render_prompt.load_issues")
@patch("render_prompt.get_parallel_agents")
@patch("sys.stdout", new_callable=MagicMock)
def test_dry_run_shows_slot_values(
    mock_stdout: Any, mock_pa: Any, mock_li: Any, mock_ls: Any
) -> None:
    mock_ls.return_value = {
        "slice-foo": MockSlice(
            "ready",
            ["spec.md"],
            ["src/foo.py"],
            [],
            [],
            REPO_ROOT / "docs/architecture/_slices/slice-foo.md",
        )
    }
    mock_li.return_value = {"slice-foo": MockIssue(42)}
    mock_pa.return_value = []

    render_prompt("gemini", "slice-foo", "~/clone", "slice-start", False, True)

    out = "".join(call[0][0] for call in mock_stdout.write.call_args_list)
    assert "DRY RUN DATA" in out
    assert "slice_id: slice-foo" in out


@patch("render_prompt.load_slices")
@patch("render_prompt.load_issues")
@patch("render_prompt.get_parallel_agents")
@patch("sys.stdout", new_callable=MagicMock)
def test_json_output_produces_valid_json(
    mock_stdout: Any, mock_pa: Any, mock_li: Any, mock_ls: Any
) -> None:
    mock_ls.return_value = {
        "slice-foo": MockSlice(
            "ready",
            ["spec.md"],
            ["src/foo.py"],
            [],
            [],
            REPO_ROOT / "docs/architecture/_slices/slice-foo.md",
        )
    }
    mock_li.return_value = {"slice-foo": MockIssue(42)}
    mock_pa.return_value = []

    render_prompt("gemini", "slice-foo", "~/clone", "slice-start", True, False)

    out = "".join(call[0][0] for call in mock_stdout.write.call_args_list)
    data = json.loads(out)
    assert data["slice_id"] == "slice-foo"


@patch("render_prompt.load_slices")
@patch("render_prompt.load_issues")
def test_template_file_missing_emits_actionable_error(mock_li: Any, mock_ls: Any) -> None:
    mock_ls.return_value = {
        "slice-foo": MockSlice(
            "ready",
            ["spec.md"],
            ["src/foo.py"],
            [],
            [],
            REPO_ROOT / "docs/architecture/_slices/slice-foo.md",
        )
    }
    mock_li.return_value = {"slice-foo": MockIssue(42)}
    with pytest.raises(SystemExit):
        render_prompt("gemini", "slice-foo", "~/clone", "missing-template-xyz", False, False)
