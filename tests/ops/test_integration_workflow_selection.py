"""Regression contract for Issue #800 integration-test discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "integration.yml"
MAKEFILE = ROOT / "Makefile"
SCHEDULED_QUALITY_NODE = (
    "tests/retrieve/test_hybrid.py::"
    "test_integration_beir_style_eval_on_1000_doc_synthetic_corpus_"
    "hybrid_beats_dense_only_by_2_ndcg10_points"
)


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
    selection = command.split(" -m integration ", 1)[0]
    assert ".py" not in selection, "workflow must not maintain a per-file pytest inventory"


def test_workflow_has_one_named_scheduled_quality_exclusion() -> None:
    command = _integration_run_command()
    assert command.count("--deselect=") == 1
    assert f"--deselect={SCHEDULED_QUALITY_NODE}" in command
    workflow_text = WORKFLOW.read_text()
    assert "dedicated scheduled x86 quality gate" in workflow_text


def test_workflow_loads_live_stack_settings_before_marker_wide_suite() -> None:
    command = _integration_run_command()
    source_index = command.index(". deploy/test-env/.env.test")
    port_index = command.index('export QDRANT_PORT="$MUSUBI_TEST_QDRANT_PORT"')
    pytest_index = command.index("uv run pytest tests/")
    assert source_index < port_index < pytest_index


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
    selection = target.split(" -m integration", 1)[0]
    assert ".py" not in selection, "local target must not maintain a per-file pytest inventory"
    assert target.count("--deselect=") == 1
    assert f"--deselect={SCHEDULED_QUALITY_NODE}" in target


def test_local_integration_target_loads_settings_and_maps_custom_qdrant_port() -> None:
    makefile = MAKEFILE.read_text()
    target = makefile.split("test-integration:", 1)[1]
    target = target.split("\n\n", 1)[0]
    source_index = target.index(". deploy/test-env/.env.test")
    port_index = target.index("export QDRANT_PORT=$$MUSUBI_TEST_QDRANT_PORT")
    pytest_index = target.index("uv run pytest")
    assert source_index < port_index < pytest_index
