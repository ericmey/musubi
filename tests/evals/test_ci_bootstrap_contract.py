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


# --- RET-004 successor: the scheduled x86 TEI live-gate job -------------------------------------


def _assert_scheduled_contract(content: str) -> None:
    parsed = yaml.safe_load(content)
    jobs = parsed.get("jobs", {})
    scheduled = jobs.get("scheduled")
    assert scheduled is not None, "Workflow must have a 'scheduled' job for the live quality gate"

    steps = scheduled.get("steps", [])
    boot_idx = -1
    live_gate_idx = -1
    for idx, step in enumerate(steps):
        run_cmd = step.get("run", "")
        if not run_cmd:
            continue
        assert "curl " not in run_cmd, "Must not use curl installer"
        assert "uv pip install -e ." not in run_cmd, "Must not use raw pip install"
        # The load-bearing stack boot: docker compose up against the test-env compose file.
        if "docker compose" in run_cmd and "up" in run_cmd and "docker-compose.test.yml" in run_cmd:
            boot_idx = idx
        # The live scheduled gate itself.
        if "musubi.evals scheduled" in run_cmd or "musubi-evals scheduled" in run_cmd:
            live_gate_idx = idx

    assert boot_idx != -1, (
        "Scheduled job MUST boot the real Qdrant+TEI stack (docker compose up) — the live gate fails "
        "loud without it and could never produce real numbers"
    )
    assert live_gate_idx != -1, "Scheduled job MUST run the live scheduled gate"
    assert boot_idx < live_gate_idx, (
        "The real stack must be booted BEFORE the live gate runs — otherwise the gate fails loud and "
        "can never produce real numbers"
    )


def test_scheduled_workflow_live_gate_contract() -> None:
    """Contract: .github/workflows/evals.yml has a scheduled job that boots the real Qdrant+TEI
    stack before running the live gate — the only place real quality numbers are produced."""
    repo_root = Path(__file__).parent.parent.parent
    content = (repo_root / ".github" / "workflows" / "evals.yml").read_text(encoding="utf-8")
    _assert_scheduled_contract(content)


def test_scheduled_workflow_discriminator_no_stack_boot() -> None:
    """Contract: a scheduled job that runs the live gate WITHOUT booting the stack is rejected — the
    exact fail-loud-forever / never-real-numbers trap."""
    no_boot = """
jobs:
  scheduled:
    steps:
      - name: Run Scheduled Live Quality Gate
        run: uv run python -m musubi.evals scheduled --data-dir tests/evals/data
    """
    with pytest.raises(AssertionError, match="MUST boot the real Qdrant"):
        _assert_scheduled_contract(no_boot)


def test_scheduled_workflow_discriminator_boot_after_gate() -> None:
    """Contract: booting the stack AFTER the gate already ran is rejected — order is load-bearing."""
    wrong_order = """
jobs:
  scheduled:
    steps:
      - name: Run Scheduled Live Quality Gate
        run: uv run python -m musubi.evals scheduled --data-dir tests/evals/data
      - name: Boot stack
        run: docker compose -f deploy/test-env/docker-compose.test.yml up -d --wait
    """
    with pytest.raises(AssertionError, match="booted BEFORE"):
        _assert_scheduled_contract(wrong_order)


# --- On-demand dispatch of the live scheduled gate -------------------------------------------------


def _assert_dispatch_contract(content: str) -> None:
    """The live scheduled gate must be dispatchable on demand AND never run on pull_request.

    - ``workflow_dispatch`` is exposed on the workflow.
    - EVERY step that runs ``musubi.evals scheduled`` (across ALL jobs — collected, never overwritten,
      so YAML order can't hide a bad match) carries at least one ``github.event_name`` guard, so the
      live gate can't fire on a PR.
    - EVERY applicable event guard (GitHub applies BOTH the job guard and the step guard) permits BOTH
      schedule AND workflow_dispatch.
    """
    parsed = yaml.safe_load(content)
    # PyYAML parses the ``on:`` key as the YAML 1.1 boolean True — accept either.
    triggers = parsed.get("on", parsed.get(True, {})) or {}
    assert "workflow_dispatch" in triggers, (
        "evals.yml must expose workflow_dispatch on the default branch so the live scheduled gate can "
        "be dispatched on demand pre-merge"
    )

    # Collect EVERY scheduled-gate step across ALL jobs — never overwrite, or a later good match could
    # hide an earlier bad one. Each match carries its full applicable guard set (job guard + step
    # guard); GitHub requires every applicable guard to permit the event.
    matches: list[list[str]] = []
    for job in parsed.get("jobs", {}).values():
        for step in job.get("steps", []):
            run_cmd = step.get("run", "") or ""
            if "musubi.evals scheduled" in run_cmd or "musubi-evals scheduled" in run_cmd:
                matches.append([guard for guard in (job.get("if"), step.get("if")) if guard])
    assert matches, "Workflow must run the live scheduled gate somewhere"

    for guards in matches:
        event_guards = [guard for guard in guards if "github.event_name" in guard]
        # No event guard ⇒ the live gate would also run on pull_request. It must be event-gated.
        assert event_guards, (
            "the scheduled gate must carry a github.event_name guard (job-level or step-level) so it "
            "never runs on pull_request; gate it on schedule OR workflow_dispatch"
        )
        # Every applicable event guard must permit BOTH the nightly cron and the on-demand dispatch.
        for guard in event_guards:
            assert "schedule" in guard and "workflow_dispatch" in guard, (
                "every event guard on the scheduled gate (job-level AND step-level) must permit BOTH "
                "schedule AND workflow_dispatch — otherwise the on-demand run or the nightly cron is "
                "silently blocked"
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


def test_evals_workflow_dispatch_discriminator_no_event_guard() -> None:
    """Contract (Copilot): a scheduled gate with NO event guard would also run on pull_request. It
    must be event-gated, never unguarded."""
    no_guard = """
on:
  workflow_dispatch:
  pull_request:
    branches: [main]
  schedule:
    - cron: '0 0 * * *'
jobs:
  smoke:
    steps:
      - name: Run Scheduled Baseline Report
        run: uv run python -m musubi.evals scheduled --data-dir tests/evals/data
    """
    with pytest.raises(AssertionError, match="never runs on pull_request"):
        _assert_dispatch_contract(no_guard)


def test_evals_workflow_dispatch_discriminator_step_guard_excludes() -> None:
    """Contract: workflow_dispatch present but the scheduled gate STEP guard is schedule-only is
    rejected — the on-demand trigger would fire nothing."""
    step_excludes = """
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
    with pytest.raises(AssertionError, match="must permit BOTH schedule AND workflow_dispatch"):
        _assert_dispatch_contract(step_excludes)


def test_evals_workflow_dispatch_discriminator_job_guard_excludes() -> None:
    """Contract (Copilot #550): a dispatch-wired STEP is still blocked when its JOB guard is
    schedule-only — GitHub requires BOTH guards to permit the event. The contract must catch the
    job-level exclusion, not just the step-level one."""
    job_excludes = """
on:
  workflow_dispatch:
  schedule:
    - cron: '0 0 * * *'
jobs:
  scheduled:
    if: github.event_name == 'schedule'
    steps:
      - name: Run Scheduled Baseline Report
        if: github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'
        run: uv run python -m musubi.evals scheduled --data-dir tests/evals/data
    """
    with pytest.raises(AssertionError, match="must permit BOTH schedule AND workflow_dispatch"):
        _assert_dispatch_contract(job_excludes)


def test_evals_workflow_dispatch_discriminator_earlier_bad_match_not_hidden() -> None:
    """Contract (Copilot): the contract must check EVERY scheduled-gate step, not just the last one.
    An earlier bad match (schedule-only) followed by a later good match must still be rejected — YAML
    order cannot hide it (proves the collect-all, no-overwrite behavior)."""
    ordered = """
on:
  workflow_dispatch:
  schedule:
    - cron: '0 0 * * *'
jobs:
  bad_first:
    steps:
      - name: Run Scheduled Baseline Report (bad)
        if: github.event_name == 'schedule'
        run: uv run python -m musubi.evals scheduled --data-dir tests/evals/data
  good_second:
    steps:
      - name: Run Scheduled Baseline Report (good)
        if: github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'
        run: uv run python -m musubi.evals scheduled --data-dir tests/evals/data
    """
    with pytest.raises(AssertionError, match="must permit BOTH schedule AND workflow_dispatch"):
        _assert_dispatch_contract(ordered)
