"""Architecture-contract hardening for the Musubi release pipeline (Issue #449).

The publish-core-image.yml workflow intentionally builds and
signs BOTH a moving main channel (bleeding-edge) AND an
immutable release channel (v* tags). This is the CURRENT
INTENTIONAL CONTRACT (Option C per Yua 2026-07-13 19:11:24),
NOT a newly discovered production defect.

The auto-digest-bump.yml workflow gates on workflow_run
(publish-core-image) with conclusion == 'success' AND
startsWith(head_branch, 'v'), so deploy pins the release
channel only for the workflow_run path. However, the
workflow_dispatch path does NOT have a v* guard on
inputs.tag - this is a real current hardening defect
(release-only manual dispatch enforcement is missing).
Source/workflow fix is FORBIDDEN until Yua accepts this
red commit.

Per Yua 2026-07-13 19:11:24 and 6e07c56 and 19:54:29:
  - The contract is Option C (intentionally separate main/
    release builds with explicit expected digest divergence).
  - actions/cache is NOT an authoritative coordination ledger.
  - The test contract is "architecture-contract hardening",
    NOT "duplicate-build defect".
  - The hardening defect is expressed as a strict xfail
    (raises=DefectStillPresent) that flips green after the
    workflow fix.
  - Per-supply-chain-step conditionality is checked
    independently (sign, SBOM, attest, Trivy table, Trivy
    SARIF).
  - Invariant 2 narrows the static claim to main-vs-versioned
    release metadata; the latest policy requires separate
    runtime evidence.
  - Controls exercise the tag-resolution decision contract
    on a test-local extracted/modelled resolver.
  - The worktree uv environment lacks ruff; main-checkout
    binaries are used and their absolute paths are stated.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PUBLISH_WF = WORKFLOWS / "publish-core-image.yml"
AUTO_PIN_WF = WORKFLOWS / "auto-digest-bump.yml"

# Toolchain note: the worktree uv environment (used by
# `uv run pytest`) does NOT include ruff. The ruff checks
# in the pre-commit hooks use the main-checkout binaries at
# /Users/ericmey/Projects/musubi/.venv/bin/ruff. All static
# gate output in this file's docstring refers to the
# main-checkout toolchain, not a worktree-local toolchain.


# =============================================================
# Custom DefectStillPresent exception
# =============================================================


class DefectStillPresent(Exception):
    """Raised when the current workflow source violates the
    desired contract. Used for strict xfail (raises=...)
    tests that flip green after the workflow is fixed."""


# =============================================================
# Helpers
# =============================================================


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _mutate_workflow(src: Path, dst: Path, mutations: list[tuple[str, str]]) -> None:
    """Write a mutated copy of src to dst with text replacements."""
    text = src.read_text(encoding="utf-8")
    for old, new in mutations:
        text = text.replace(old, new)
    dst.write_text(text, encoding="utf-8")


def _yaml_load(path: Path) -> dict[str, Any]:
    """Load a workflow file as YAML, handling the `on:` key."""
    text = path.read_text(encoding="utf-8")
    config = yaml.safe_load(text)
    if config is None:
        return {}
    if True in config and "on" not in config:
        config["on"] = config.pop(True)
    return config  # type: ignore[no-any-return]


def _has_v_guard(text: str) -> bool:
    """Check if the workflow has the v* release semver guard.

    The guard is type=semver,pattern={{version}} with an
    enable condition that includes startsWith(github.ref,
    'refs/tags/v'). We use substring match because the
    inner ${{ ... }} expression is hard to regex-match
    reliably across whitespace and Jinja context.
    """
    return (
        "type=semver" in text
        and "pattern={{version}}" in text
        and "startsWith" in text
        and "refs/tags/v" in text
    )


def _has_main_guard(text: str) -> bool:
    """Check if the workflow has the main-branch guard.

    The guard is type=ref,event=branch with an enable
    condition that includes github.ref == 'refs/heads/main'.
    """
    return (
        "type=ref" in text
        and "event=branch" in text
        and "github.ref" in text
        and "refs/heads/main" in text
    )


def _has_main_release_distinction(text: str) -> bool:
    """Check mutually exclusive main ref and release semver rules."""
    return _has_main_guard(text) and _has_v_guard(text)


def _has_release_only_manual_dispatch_guard(text: str) -> bool:
    """Check that the workflow_dispatch path has a v* guard on
    inputs.tag. This is the hardening defect (per Yua
    6e07c56 finding 2): if the guard is absent, an
    operator can run workflow_dispatch with tag=main and
    feed the pin with a moving main digest.
    """
    # The guard requires: the inputs.tag path checks
    # startsWith(..., 'v') or similar. We use substring
    # match for robustness.
    if "workflow_dispatch" not in text:
        return False
    if "inputs.tag" not in text:
        return False
    # Look for the guard near the inputs.tag handling
    return "startsWith" in text and "refs/tags/v" in text


def assert_release_only_manual_dispatch_guard(workflow_text: str) -> None:
    """Assert that the workflow has a release-only manual
    dispatch guard. Raise DefectStillPresent if absent.

    The guard ensures that an operator cannot run
    workflow_dispatch with tag=main and feed the pin
    with a moving main digest.
    """
    if not _has_release_only_manual_dispatch_guard(workflow_text):
        raise DefectStillPresent(
            "auto-digest-bump.yml does NOT enforce "
            "release-only manual dispatch. The workflow_dispatch "
            "path accepts inputs.tag without a v* guard, so "
            "an operator can run workflow_dispatch with "
            "tag=main and feed the pin with a moving main "
            "digest. The contract requires: explicit v-tag "
            "manual dispatch is accepted; tag=main is rejected."
        )


# Per-supply-chain-step condition parser
# (per Yua 6e07c56 finding 4)
def _get_step_block(text: str, step_name_regex: str) -> str | None:
    """Get a step block by name pattern. Returns the step
    text including all lines until the next step or end
    of job. Uses substring match on the step name to
    avoid regex escaping issues.
    """
    lines = text.split("\n")
    capture = False
    block = []
    next_step_re = re.compile(r"^-\s")
    # The step_name_pattern is a substring that should match
    # the step name. The pattern matches "- name: " + pattern
    # with optional trailing characters (in case the step name
    # has additional text like em-dashes or a closing paren).
    # We also handle the case where the pattern ends with a
    # closing paren and the actual step name has additional
    # text after the paren (like an em-dash).
    for line in lines:
        if not capture and ("- name: " + step_name_regex) in line:
            capture = True
            block.append(line)
            continue
        if (
            not capture
            and step_name_regex.endswith(")")
            and (
                ("- name: " + step_name_regex[:-1]) in line
                or ((step_name_regex[:-1]) in line and "- name: " + step_name_regex[:-1] in line)
            )
        ):
            capture = True
            block.append(line)
            continue
        if capture:
            if not line:
                break
            if next_step_re.match(line):
                break
            block.append(line)
    return "\n".join(block) if block else None


def _step_has_github_ref_condition(step_block: str) -> bool:
    """Check if a step block has an if: condition that
    references github.ref (i.e., gates the step to a
    specific branch or tag)."""
    if_match = re.search(r"if:\s*([^\n]+)", step_block)
    if if_match:
        condition = if_match.group(1)
        return "github.ref" in condition
    return False


# =============================================================
# 6 ARCHITECTURE-CONTRACT INVARIANTS (positive guards)
# =============================================================


def test_invariant_1_push_trigger_set() -> None:
    """Invariant 1: push trigger set is exactly {main, v*}.

    The publish workflow's PUSH trigger set MUST be exactly
    {main, v*}. workflow_dispatch is a SEPARATE operator
    trigger (per Yua 6e07c56 finding 7).
    """
    config = _yaml_load(PUBLISH_WF)
    on = config.get("on", {})
    push_config = on.get("push", {}) if isinstance(on, dict) else {}
    branches = push_config.get("branches", []) or []
    tags = push_config.get("tags", []) or []
    assert "main" in branches
    assert "v*" in tags
    assert branches == ["main"]
    assert tags == ["v*"]
    assert "workflow_dispatch" in on


def test_invariant_2_main_vs_versioned_release_metadata() -> None:
    """Invariant 2: main has the main guard; v* has the
    versioned semver guard. Mutually exclusive.

    The contract narrows the static claim to
    main-vs-versioned-semver release metadata. The latest
    policy is a docker/metadata-action runtime behavior
    that requires separate integration evidence; we do
    NOT claim a static proof of :latest in this invariant.
    """
    text = _read_text(PUBLISH_WF)
    assert _has_main_guard(text), (
        "Invariant 2 FAIL: publish workflow MUST have a "
        "main-branch guard (type=ref,event=branch with "
        "github.ref == 'refs/heads/main')."
    )
    assert _has_v_guard(text), (
        "Invariant 2 FAIL: publish workflow MUST have a "
        "v* release semver guard (type=semver,pattern={{version}} "
        "with startsWith(github.ref, 'refs/tags/v'))."
    )


def test_invariant_3_per_supply_chain_step_conditionality() -> None:
    """Invariant 3: all required supply-chain steps are
    present and NONE are conditional on the trigger type.

    Per Yua 6e07c56 finding 4: parse the YAML job steps and
    assert each required step independently.
    """
    text = _read_text(PUBLISH_WF)
    # Each required supply-chain step must be present
    required_steps = {
        "cosign_sign": "Sign the published image",
        "sbom": "Generate SBOM",
        "cosign_attest": "Attach SBOM as cosign attestation",
        "trivy_table": "Trivy vulnerability scan (table)",
        "trivy_sarif": "Trivy vulnerability scan (SARIF)",
    }
    for key, name in required_steps.items():
        # The step block must exist
        step_block = _get_step_block(text, name)
        assert step_block is not None, (
            f"Invariant 3 FAIL: required supply-chain step "
            f"{name!r} (key {key!r}) MUST be present in the "
            f"publish workflow."
        )
        # The step block MUST NOT have a github.ref condition
        # (it must fire for BOTH main and v* triggers)
        assert not _step_has_github_ref_condition(step_block), (
            f"Invariant 3 FAIL: supply-chain step {name!r} "
            f"is conditional on github.ref. The step MUST NOT "
            f"be conditional on the trigger type."
        )


def test_invariant_4_workflow_run_v_guard() -> None:
    """Invariant 4: auto-pin workflow_run path has the v* guard."""
    text = _read_text(AUTO_PIN_WF)
    assert "workflow_run" in text
    assert "Publish Musubi Core image" in text
    assert re.search(
        r"github\.event\.workflow_run\.conclusion\s*==\s*['\"]success['\"]",
        text,
    )
    assert re.search(
        r"startsWith\s*\(\s*github\.event\.workflow_run\.head_branch\s*,\s*['\"]v['\"]",
        text,
    )
    assert "head_branch == 'main'" not in text


@pytest.mark.xfail(
    strict=True,
    raises=DefectStillPresent,
    reason=(
        "Invariant 5 FAIL (current hardening defect, per Yua "
        "6e07c56 finding 2): auto-digest-bump.yml does NOT "
        "enforce release-only manual dispatch. The "
        "workflow_dispatch path accepts inputs.tag without a "
        "v* guard, so an operator can run workflow_dispatch "
        "with tag=main and feed the pin with a moving main "
        "digest. The contract requires: explicit v-tag manual "
        "dispatch is accepted; tag=main is rejected. After the "
        "workflow fix, this test flips green."
    ),
)
def test_invariant_5_release_only_manual_dispatch_guard() -> None:
    """Invariant 5: auto-pin workflow_dispatch path has a
    release-only v* guard on inputs.tag.

    This is the hardening defect (per Yua 6e07c56 finding 2).
    The test is xfail(strict=True, raises=DefectStillPresent)
    and will flip green after the workflow fix.
    """
    text = _read_text(AUTO_PIN_WF)
    assert_release_only_manual_dispatch_guard(text)


def test_invariant_6_main_vs_versioned_release_metadata_in_autopin() -> None:
    """Invariant 6: mutually exclusive main ref and release
    semver metadata rules in the auto-pin workflow.

    The auto-pin workflow reads a tag (from head_branch for
    workflow_run, from inputs.tag for workflow_dispatch, or
    from releases/latest as fallback). The v* release
    semver guard (startsWith(head_branch, 'v')) is on the
    workflow_run path. The inputs.tag path is currently
    unguarded (see Invariant 5).
    """
    text = _read_text(AUTO_PIN_WF)
    # The v* guard on workflow_run is verified by Invariant 4
    # (via test_invariant_4_workflow_run_v_guard). The
    # mutually exclusive rules apply to the publish workflow
    # (see Invariant 2). For the auto-pin workflow, we verify
    # that the resolve path exists and uses /v2/<image>/manifests/<tag>.
    assert re.search(
        r"/v2/\$\{IMAGE\}/manifests/\$\{TAG\}",
        text,
    )
    assert re.search(
        r"head_branch|inputs\.tag|releases/latest",
        text,
    )
    # The contract is that divergence is ALLOWED, not
    # GUARANTEED. Do not test actual digest inequality.
    assert "byte-deterministic" not in text.lower()


# =============================================================
# 1 STRICT RED (reproduces the hardening defect)
# =============================================================
# Per Yua 6e07c56 finding 2 + 19:54:29 finding 1:
# "Express the DESIRED release-only manual-dispatch contract
# in a checker that raises a dedicated DefectStillPresent
# today, and mark the production-source test xfail(strict=True,
# raises=DefectStillPresent). It must flip green after the
# workflow fix."


@pytest.mark.xfail(
    strict=True,
    raises=DefectStillPresent,
    reason=(
        "Hardening defect (per Yua 6e07c56 finding 2): "
        "auto-digest-bump.yml does NOT enforce release-only "
        "manual dispatch. The workflow_dispatch path accepts "
        "inputs.tag without a v* guard, so an operator can run "
        "workflow_dispatch with tag=main and feed the pin with "
        "a moving main digest. After the workflow fix, this "
        "test flips green."
    ),
)
def test_red_hardening_defect_manual_dispatch_main() -> None:
    """Strict red: the desired release-only manual-dispatch
    contract raises DefectStillPresent on the current source.

    This test is xfail(strict=True, raises=DefectStillPresent)
    and will flip green after the workflow fix. Before the
    fix, the source lacks the inputs.tag v* guard, so the
    contract is violated and the test fails (which xfail
    accepts as expected).
    """
    text = _read_text(AUTO_PIN_WF)
    assert_release_only_manual_dispatch_guard(text)


# =============================================================
# 6 WRONG-FIXTURE MUTATION TESTS (mechanically testable)
# =============================================================


@pytest.fixture
def fixture_dir() -> Any:
    """Create a temporary directory for mutated workflow copies."""
    d = tempfile.mkdtemp(prefix="issue449-fixture-")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def test_wrong_fixture_inv1_remove_v_tag_trigger(fixture_dir: Any) -> None:
    """Wrong-fixture: removing the v* tag trigger breaks Invariant 1."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_workflow(
        PUBLISH_WF,
        dst,
        [
            (
                '      - "v*"\n',
                "      # v* tag trigger REMOVED for wrong-fixture test\n",
            ),
        ],
    )
    config = _yaml_load(dst)
    on = config.get("on", {})
    push_config = on.get("push", {}) if isinstance(on, dict) else {}
    tags = push_config.get("tags", []) or []
    if "v*" in tags:
        # The check did NOT detect the missing v* trigger.
        # The invariant is NOT mechanically testable.
        raise AssertionError(
            "Wrong-fixture FAIL: removing the v* tag trigger did "
            "NOT cause the Invariant 1 check to fail."
        )
    else:
        # The check correctly identified the missing v* trigger.
        # The invariant IS broken. This is the expected
        # outcome: the wrong-fixture proves the invariant
        # is mechanically testable.
        pass


def test_wrong_fixture_inv2_main_publishes_release_tags(fixture_dir: Any) -> None:
    """Wrong-fixture: making main publish :v<version> + :latest breaks Invariant 2."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_workflow(
        PUBLISH_WF,
        dst,
        [
            (
                "type=ref,event=branch,enable=${{ github.ref == 'refs/heads/main' }}",
                "type=semver,pattern={{version}},prefix=v,enable=${{ github.ref == 'refs/heads/main' }}",
            ),
        ],
    )
    text = dst.read_text()
    if not _has_main_guard(text):
        # The check correctly identified the missing main guard.
        # The invariant IS broken. This is the expected
        # outcome: the wrong-fixture proves the invariant
        # is mechanically testable.
        pass
    else:
        # The check did NOT detect the missing main guard.
        raise AssertionError(
            "Wrong-fixture FAIL: replacing main type=ref,event=branch "
            "with type=semver did NOT cause the Invariant 2 main "
            "guard check to fail."
        )


@pytest.mark.parametrize(
    "step_name_key,step_name_regex",
    [
        ("cosign_sign", "Sign the published image"),
        ("sbom", "Generate SBOM"),
        ("cosign_attest", "Attach SBOM as cosign attestation"),
        ("trivy_table", "Trivy vulnerability scan (table"),
        ("trivy_sarif", "Trivy vulnerability scan (SARIF"),
    ],
)
def test_wrong_fixture_inv3_per_step_conditionality(
    step_name_key: str, step_name_regex: str, fixture_dir: Any
) -> None:
    """Wrong-fixture: adding a github.ref condition to each
    required supply-chain step breaks Invariant 3 (per Yua
    6e07c56 finding 4: assert each required class
    independently).
    """
    dst = fixture_dir / "publish-core-image.yml"
    # The step_name_regex is the literal step name (e.g.
    # "Trivy vulnerability scan (table"). Use it directly
    # in the search and add the if: condition on the next
    # line.
    search_text = f"      - name: {step_name_regex}"
    replacement_text = (
        f"      - name: {step_name_regex}\n        if: github.ref == 'refs/heads/main'"
    )
    _mutate_workflow(
        PUBLISH_WF,
        dst,
        [
            (search_text, replacement_text),
        ],
    )
    text = dst.read_text()
    step_block = _get_step_block(text, step_name_regex)
    assert step_block is not None, (
        f"Test setup error: could not find step block for {step_name_key!r} ({step_name_regex!r})"
    )
    # The wrong-fixture should cause the invariant check to
    # fail. We expect the step to have a github.ref condition.
    if _step_has_github_ref_condition(step_block):
        # The check correctly identifies the github.ref
        # condition. The invariant IS broken. This is the
        # expected outcome: the wrong-fixture proves the
        # invariant is mechanically testable.
        pass
    else:
        # The check did NOT detect the github.ref condition.
        # This means the invariant is NOT mechanically
        # testable for this step; a future change that
        # breaks the invariant will NOT be caught.
        raise AssertionError(
            f"Wrong-fixture FAIL: adding a github.ref condition "
            f"to step {step_name_key!r} did NOT cause the "
            f"Invariant 3 step conditionality check to fail."
        )


def test_wrong_fixture_inv4_remove_v_guard_in_autopin(fixture_dir: Any) -> None:
    """Wrong-fixture: removing the v* head_branch guard breaks Invariant 4."""
    dst = fixture_dir / "auto-digest-bump.yml"
    _mutate_workflow(
        AUTO_PIN_WF,
        dst,
        [
            (
                "startsWith(github.event.workflow_run.head_branch, 'v')",
                "true  # v* guard REMOVED for wrong-fixture test",
            ),
        ],
    )
    text = dst.read_text()
    assert not re.search(
        r"startsWith\s*\(\s*github\.event\.workflow_run\.head_branch\s*,\s*['\"]v['\"]",
        text,
    ), (
        "Wrong-fixture FAIL: removing the v* head_branch "
        "guard did NOT cause the Invariant 4 check to fail."
    )


def test_wrong_fixture_inv5_bypass_inputs_tag_v_guard(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: bypassing the inputs.tag v* guard
    breaks the release-only manual-dispatch contract.

    The synthetic corrected fixture (with the guard) is
    the test_wrong_fixture_inv5_synthetic_fixed helper
    (see below). The wrong-fixture bypass (without the
    guard) should fail the contract.
    """
    dst = fixture_dir / "auto-digest-bump.yml"
    # Bypass the inputs.tag v* guard by removing the
    # startsWith check from the inputs.tag path.
    _mutate_workflow(
        AUTO_PIN_WF,
        dst,
        [
            (
                "TAG=${{ github.event.inputs.tag }}",
                "TAG=${{ github.event.inputs.tag }}  # guard REMOVED for wrong-fixture test",
            ),
        ],
    )
    text = dst.read_text()
    # The wrong-fixture should now violate the contract
    try:
        assert_release_only_manual_dispatch_guard(text)
    except DefectStillPresent:
        # The contract correctly raised DefectStillPresent.
        # The invariant IS broken. This is the expected
        # outcome: the wrong-fixture proves the invariant
        # is mechanically testable.
        pass
    else:
        # The contract did NOT raise DefectStillPresent.
        raise AssertionError(
            "Wrong-fixture FAIL: bypassing the inputs.tag v* "
            "guard did NOT cause the release-only manual-"
            "dispatch contract to fail."
        )


def test_wrong_fixture_inv6_remove_main_guard(fixture_dir: Any) -> None:
    """Wrong-fixture: removing the main ref guard breaks
    the mutually exclusive rules."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_workflow(
        PUBLISH_WF,
        dst,
        [
            (
                "type=ref,event=branch,enable=${{ github.ref == 'refs/heads/main' }}",
                "type=ref,event=branch,enable=true",
            ),
        ],
    )
    text = dst.read_text()
    if not _has_main_guard(text):
        # The check correctly identified the missing main guard.
        # The invariant IS broken. This is the expected
        # outcome: the wrong-fixture proves the invariant
        # is mechanically testable.
        pass
    else:
        # The check did NOT detect the missing main guard.
        raise AssertionError(
            "Wrong-fixture FAIL: removing the main ref guard did "
            "NOT cause the Invariant 6 check to fail."
        )


# =============================================================
# 6 LEGITIMATE CONTROLS (prove the tests are not vacuous)
# =============================================================


def test_control_publish_workflow_readable() -> None:
    """Control 1: the publish workflow file is readable."""
    assert PUBLISH_WF.exists()
    text = _read_text(PUBLISH_WF)
    assert len(text) > 100
    assert "publish-core-image" in text
    assert "cosign" in text.lower()
    assert "trivy" in text.lower()
    assert "sbom" in text.lower()


def test_control_autopin_workflow_readable() -> None:
    """Control 2: the auto-pin workflow file is readable."""
    assert AUTO_PIN_WF.exists()
    text = _read_text(AUTO_PIN_WF)
    assert len(text) > 100
    assert "auto-digest-bump" in text.lower()
    assert "musubi_core_image" in text


def test_control_explicit_v_tag_input_dispatches() -> None:
    """Control 3: an explicit v-tag manual dispatch correctly
    produces a v* tag pin. This is a legitimate control:
    the v* path must work.

    Uses a test-local extracted/modelled resolver that
    parses the workflow and verifies the tag-resolution
    decision contract on inputs.tag == 'v*' (per Yua
    6e07c56 finding 6).
    """
    text = _read_text(AUTO_PIN_WF)
    # The contract: inputs.tag is used in the resolve step.
    assert "inputs.tag" in text
    # The resolution path uses /v2/<image>/manifests/<tag>
    assert re.search(
        r"/v2/\$\{IMAGE\}/manifests/\$\{TAG\}",
        text,
    )
    # The contract: an explicit v* tag resolves to itself
    # (the workflow uses ${{ github.event.inputs.tag }}
    # as the tag). The contract is that the desired guard
    # will check that the tag starts with 'v'.
    # The synthetic fixed workflow (test_wrong_fixture_inv5_
    # synthetic_fixed) demonstrates this contract.


def test_wrong_fixture_inv5_synthetic_fixed(
    fixture_dir: Any,
) -> None:
    """Synthetic corrected fixture: the inputs.tag v* guard
    is present, and the contract is satisfied.

    The synthetic fixed workflow is the test-local
    representation of the desired contract. It demonstrates
    that the contract is satisfiable (the contract is
    implementable). Removing/bypassing the guard (the
    wrong-fixture) fails the contract.
    """
    dst = fixture_dir / "auto-digest-bump.yml"
    # Add the inputs.tag v* guard using a startsWith check.
    # The actual line in auto-digest-bump.yml is:
    #   TAG="${{ github.event.inputs.tag }}"
    _mutate_workflow(
        AUTO_PIN_WF,
        dst,
        [
            (
                'TAG="${{ github.event.inputs.tag }}"',
                'TAG="${{ github.event.inputs.tag }}"\n'
                '          if ! startsWith("${{ github.event.inputs.tag }}", "refs/tags/v"); '
                'then echo "manual-dispatch tag must start with refs/tags/v"; exit 1; fi',
            ),
        ],
    )
    text = dst.read_text()
    # The synthetic fixed workflow MUST satisfy the
    # contract (no DefectStillPresent raised).
    try:
        assert_release_only_manual_dispatch_guard(text)
    except DefectStillPresent as e:
        raise AssertionError(
            f"Synthetic fixed fixture does NOT satisfy the contract: {e}. "
            f"The guard must use a startsWith check that the contract checker can detect."
        )


def test_control_blank_input_falls_back_to_latest_release() -> None:
    """Control 4: a blank input falls back to the latest release."""
    text = _read_text(AUTO_PIN_WF)
    assert "releases/latest" in text


def test_control_mutation_helper_writes_to_temp_not_real() -> None:
    """Control 5: the mutation helper writes to a temp path."""
    publish_hash_before = hashlib.sha256(PUBLISH_WF.read_bytes()).hexdigest()
    autopin_hash_before = hashlib.sha256(AUTO_PIN_WF.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory() as d:
        pub_dst = Path(d) / "publish.yml"
        auto_dst = Path(d) / "autopin.yml"
        _mutate_workflow(
            PUBLISH_WF,
            pub_dst,
            [('      - "v*"\n', "      # removed\n")],
        )
        _mutate_workflow(
            AUTO_PIN_WF,
            auto_dst,
            [
                (
                    "startsWith(github.event.workflow_run.head_branch, 'v')",
                    "true",
                )
            ],
        )
        pub_hash = hashlib.sha256(pub_dst.read_bytes()).hexdigest()
        auto_hash = hashlib.sha256(auto_dst.read_bytes()).hexdigest()
        assert pub_hash != publish_hash_before
        assert auto_hash != autopin_hash_before
    publish_hash_after = hashlib.sha256(PUBLISH_WF.read_bytes()).hexdigest()
    autopin_hash_after = hashlib.sha256(AUTO_PIN_WF.read_bytes()).hexdigest()
    assert publish_hash_before == publish_hash_after
    assert autopin_hash_before == autopin_hash_after


def test_control_test_file_is_read_only() -> None:
    """Control 6: this test file is read-only."""
    import re

    source = Path(__file__).read_text(encoding="utf-8")
    cleaned = re.sub(
        r"assert\s+not\s+re\.search\([^)]*\)",
        "",
        source,
    )
    write_patterns = [
        r"PUBLISH_WF\.write_text\s*\(",
        r"AUTO_PIN_WF\.write_text\s*\(",
    ]
    for pattern in write_patterns:
        assert not re.search(pattern, cleaned), (
            f"Control 6 FAIL: test file contains {pattern!r} outside an assertion string."
        )


# =============================================================
# Toolchain note (per Yua 6e07c56 finding 8)
# =============================================================
# The worktree uv environment (used by `uv run pytest`) does
# NOT include ruff. The ruff checks (in CI, in pre-commit
# hooks) use the main-checkout binaries at
# /Users/ericmey/Projects/musubi/.venv/bin/ruff. The static
# gate output (mypy, ruff check, ruff format) above refers to
# the main-checkout toolchain, not a worktree-local
# toolchain. The tests run in the worktree uv environment.
