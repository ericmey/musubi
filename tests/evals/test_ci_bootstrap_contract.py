import os
import sys
from pathlib import Path

import pytest
import yaml


def test_uv_virtualenv_active() -> None:
    """
    Contract: The CI runner MUST execute within a properly isolated virtual environment.
    """
    if not os.environ.get("CI"):
        pytest.skip("CI-only environment contract")
    assert sys.prefix != sys.base_prefix, "CI execution MUST be isolated in a virtual environment"
    assert "VIRTUAL_ENV" in os.environ, "VIRTUAL_ENV environment variable MUST be present"


def _assert_workflow_contract(content: str) -> None:
    parsed = yaml.safe_load(content)

    jobs = parsed.get("jobs", {})
    smoke_job = jobs.get("smoke")
    assert smoke_job is not None, "Workflow must have a 'smoke' job"

    steps = smoke_job.get("steps", [])

    # Extract structural order
    setup_uv_idx = -1
    uv_python_idx = -1
    uv_sync_idx = -1
    smoke_gate_idx = -1

    for idx, step in enumerate(steps):
        # Reject broken commands globally in run blocks
        run_cmd = step.get("run", "")
        if run_cmd:
            assert "curl " not in run_cmd, "Must not use curl installer"
            assert "uv pip install -e ." not in run_cmd, "Must not use raw pip install"
            assert "--system" not in run_cmd, "Must not use system python flag"

            # Extract exact non-comment command lines
            normalized_lines = [line.strip() for line in run_cmd.splitlines()]
            executable_lines = [
                line for line in normalized_lines if line and not line.startswith("#")
            ]

            if "uv python install 3.12" in executable_lines:
                uv_python_idx = idx
            if "uv sync --extra dev" in executable_lines:
                uv_sync_idx = idx

        if step.get("uses") == "astral-sh/setup-uv@v8.1.0":
            setup_uv_idx = idx

        if step.get("name") == "Run PR Smoke Gate":
            smoke_gate_idx = idx

    assert setup_uv_idx != -1, "Must use canonical setup-uv action"
    assert uv_python_idx != -1, "Must use canonical uv python install"
    assert uv_sync_idx != -1, "Must use canonical uv sync"
    assert smoke_gate_idx != -1, "Must have PR Smoke Gate step"

    assert setup_uv_idx < uv_python_idx < uv_sync_idx < smoke_gate_idx, (
        "Bootstrap steps must occur in order before the smoke gate"
    )


def test_evals_workflow_file_contract() -> None:
    """Contract: .github/workflows/evals.yml MUST use the canonical setup-uv pattern structurally."""
    repo_root = Path(__file__).parent.parent.parent
    workflow_path = repo_root / ".github" / "workflows" / "evals.yml"
    assert workflow_path.exists(), "evals.yml must exist"

    content = workflow_path.read_text(encoding="utf-8")
    _assert_workflow_contract(content)


def test_evals_workflow_discriminator_prior_broken_state() -> None:
    """Contract: Prove that the broken prior workflow text fails."""
    broken_content = """
jobs:
  smoke:
    steps:
      - name: Setup Python
        uses: actions/setup-python@v5
      - name: Install uv and deps
        run: |
          curl -LsSf https://astral.sh/uv/install.sh | sh
          uv pip install -e .
      - name: Run PR Smoke Gate
        run: uv run python -m musubi.evals smoke
    """
    with pytest.raises(AssertionError, match="Must not use curl installer"):
        _assert_workflow_contract(broken_content)


def test_evals_workflow_discriminator_decoy() -> None:
    """Contract: Prove that decoy text in comments or wrong jobs fails the structural assertion."""
    decoy_content = """
# astral-sh/setup-uv@v8.1.0
# uv python install 3.12
# uv sync --extra dev
jobs:
  other_job:
    steps:
      - uses: astral-sh/setup-uv@v8.1.0
      - run: uv python install 3.12
      - run: uv sync --extra dev
  smoke:
    steps:
      - name: Run PR Smoke Gate
        run: echo "No setup here"
    """
    with pytest.raises(AssertionError, match="Must use canonical setup-uv action"):
        _assert_workflow_contract(decoy_content)


def test_evals_workflow_discriminator_wrong_order() -> None:
    """Contract: Prove that correct commands in the wrong order (e.g. after smoke gate) are rejected."""
    wrong_order = """
jobs:
  smoke:
    steps:
      - name: Run PR Smoke Gate
        run: echo "gate runs first"
      - uses: astral-sh/setup-uv@v8.1.0
      - run: uv python install 3.12
      - run: uv sync --extra dev
    """
    with pytest.raises(
        AssertionError, match="Bootstrap steps must occur in order before the smoke gate"
    ):
        _assert_workflow_contract(wrong_order)


def test_evals_workflow_discriminator_echo_decoy() -> None:
    """Contract: Prove that echo decoys inside run blocks fail the exact-line assertion."""
    decoy_content = """
jobs:
  smoke:
    steps:
      - uses: astral-sh/setup-uv@v8.1.0
      - run: echo uv python install 3.12
      - run: echo uv sync --extra dev
      - name: Run PR Smoke Gate
        run: echo gate
    """
    with pytest.raises(AssertionError, match="Must use canonical uv python install"):
        _assert_workflow_contract(decoy_content)


# --- On-demand dispatch of the live scheduled gate -------------------------------------------------


def _assert_dispatch_contract(content: str) -> None:
    """The live scheduled gate must be dispatchable on demand: ``workflow_dispatch`` present, and the
    step that runs ``musubi.evals scheduled`` fires for it (schedule OR workflow_dispatch) — in
    whatever job it lives (a step in ``smoke`` today, a dedicated job after the RET-004 merge)."""
    parsed = yaml.safe_load(content)
    # PyYAML parses the ``on:`` key as the YAML 1.1 boolean True — accept either.
    triggers = parsed.get("on", parsed.get(True, {})) or {}
    assert "workflow_dispatch" in triggers, (
        "evals.yml must expose workflow_dispatch on the default branch so the live scheduled gate can "
        "be dispatched on demand pre-merge"
    )

    scheduled_guard: str | None = None
    for job in parsed.get("jobs", {}).values():
        for step in job.get("steps", []):
            run_cmd = step.get("run", "") or ""
            if "musubi.evals scheduled" in run_cmd or "musubi-evals scheduled" in run_cmd:
                scheduled_guard = step.get("if") or job.get("if") or ""
    assert scheduled_guard is not None, "Workflow must run the live scheduled gate somewhere"
    assert "workflow_dispatch" in scheduled_guard, (
        "the live scheduled gate must run for workflow_dispatch (schedule OR workflow_dispatch), "
        "otherwise the on-demand trigger fires nothing"
    )


def test_evals_workflow_dispatch_enabled() -> None:
    """Contract: evals.yml exposes workflow_dispatch and wires the scheduled gate to it."""
    repo_root = Path(__file__).parent.parent.parent
    content = (repo_root / ".github" / "workflows" / "evals.yml").read_text(encoding="utf-8")
    _assert_dispatch_contract(content)


def test_evals_workflow_dispatch_discriminator_missing_trigger() -> None:
    """Contract: a workflow that runs the scheduled gate but omits the workflow_dispatch trigger is
    rejected — it could never be dispatched on demand pre-merge."""
    no_dispatch = """
on:
  schedule:
    - cron: '0 0 * * *'
jobs:
  smoke:
    steps:
      - name: Run Scheduled Baseline Report
        if: github.event_name == 'schedule'
        run: uv run python -m musubi.evals scheduled --data-dir tests/evals/data
    """
    with pytest.raises(AssertionError, match="must expose workflow_dispatch"):
        _assert_dispatch_contract(no_dispatch)


def test_evals_workflow_dispatch_discriminator_gate_not_wired() -> None:
    """Contract: workflow_dispatch present but the scheduled gate step not wired to it is rejected —
    the on-demand trigger would fire nothing."""
    unwired = """
on:
  workflow_dispatch:
  schedule:
    - cron: '0 0 * * *'
jobs:
  smoke:
    steps:
      - name: Run Scheduled Baseline Report
        if: github.event_name == 'schedule'
        run: uv run python -m musubi.evals scheduled --data-dir tests/evals/data
    """
    with pytest.raises(AssertionError, match="must run for workflow_dispatch"):
        _assert_dispatch_contract(unwired)
