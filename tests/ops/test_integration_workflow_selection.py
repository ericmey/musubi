"""Regression contract for Issue #800 integration-test discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "integration.yml"
MAKEFILE = ROOT / "Makefile"


def _workflow() -> dict[str, Any]:
    loaded = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    return cast(dict[str, Any], loaded)


def _integration_run_command() -> str:
    steps = _workflow()["jobs"]["integration"]["steps"]
    run_step = next(step for step in steps if step.get("name") == "Run integration suite")
    return cast(str, run_step["run"])


def test_workflow_selects_every_integration_marked_test_by_default() -> None:
    command = _integration_run_command()
    assert "uv run pytest tests/" in command
    assert " -m integration " in command.replace("\n", " ")
    assert ".py" not in command, "workflow must not maintain a per-file pytest inventory"


def test_pull_request_trigger_covers_new_test_files_by_default() -> None:
    paths = _workflow()["on"]["pull_request"]["paths"]
    assert "tests/**" in paths
    assert not any(path.startswith("tests/") and path.endswith(".py") for path in paths), (
        "workflow trigger must not maintain a per-file test inventory"
    )


def test_local_integration_target_uses_the_same_marker_wide_selection() -> None:
    makefile = MAKEFILE.read_text()
    target = makefile.split("test-integration:", 1)[1]
    target = target.split("\n\n", 1)[0]
    assert "tests/ -m integration" in target
    assert ".py" not in target, "local target must not maintain a per-file pytest inventory"
