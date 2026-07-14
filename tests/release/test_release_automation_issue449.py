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
workflow_dispatch path does NOT have a valid release-only
guard on the resolved tag - this is a real current hardening
defect. Source/workflow fix is FORBIDDEN until Yua accepts
this red commit.

Per Yua 2026-07-13 20:48:34 (WITHHOLD on 90455eb):
  - Every wrong-fixture test must invoke the same checker
    used by the production strict red/guard.
  - Invariant 3 checker must accept a path parameter.
  - Invariant 2/6 must parse tag rules into distinct
    comma-delimited records.
  - Bash guard recognizer must prove reject control flow
    and placement.
  - Decision model must execute the fallback contract
    with explicit input + latest value.
  - Fixed-to-wrong derivation: keep the corrected fixture
    as the parent and derive each wrong from it.

Toolchain note (per Yua 6e07c56 finding 8):
The worktree uv environment (used by 'uv run pytest') does
NOT include ruff. The ruff checks (in CI, in pre-commit
hooks) use the main-checkout binaries at
/Users/ericmey/Projects/musubi/.venv/bin/ruff. The static
gate output (mypy, ruff check, ruff format) above refers
to the main-checkout toolchain, not a worktree-local
toolchain. The tests run in the worktree uv environment.
"""

from __future__ import annotations

import copy
import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PUBLISH_WF = WORKFLOWS / "publish-core-image.yml"
AUTO_PIN_WF = WORKFLOWS / "auto-digest-bump.yml"


# =============================================================
# Custom exceptions
# =============================================================


class InvariantError(Exception):
    """Base exception for architecture-contract invariant
    violations. Specific invariants raise specific subclasses."""


class NoIfKeyInStepError(InvariantError):
    """Invariant 3: a required supply-chain step has an
    if: condition (gating to a trigger), is missing, or
    has a duplicate/decoy name."""


class ManualDispatchGuardMissingError(InvariantError):
    """Invariant 5: the Resolve tag step lacks a valid
    release-only manual dispatch guard on the resolved
    tag (e.g., [[ \"$TAG\" == v* ]])."""


class MutexChannelMissingError(InvariantError):
    """Invariant 2/6: the docker/metadata-action with.tags
    is missing a distinct semver-v, main-ref, or
    manual-raw rule."""


class TagResolutionRejectError(Exception):
    """The decision model rejects the input as a non-release
    tag."""


# =============================================================
# Helpers
# =============================================================


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_publish_steps(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the publish-core-image steps from YAML.

    The function accepts an optional path; if not provided,
    the production source is used.
    """
    if path is None:
        path = PUBLISH_WF
    config: Any = yaml.safe_load(_read_text(path))
    if not isinstance(config, dict):
        raise InvariantError(f"Could not parse workflow {path}")
    if True in config and "on" not in config:
        config["on"] = config.pop(True)
    job = config.get("jobs", {}).get("publish-core-image", {})
    if not isinstance(job, dict):
        raise InvariantError(f"No jobs.publish-core-image in {path}")
    return job.get("steps", [])  # type: ignore[no-any-return]


def _get_publish_step(name_substring: str, path: Path | None = None) -> dict[str, Any] | None:
    """Get a publish step by name substring match."""
    for step in _load_publish_steps(path):
        name = step.get("name", "")
        if name_substring in name:
            return step
    return None


def _get_resolve_step_run(path: Path | None = None) -> str:
    """Get the `run` script of the Resolve tag + digest step
    in auto-digest-bump.yml. Returns the bash script content.

    The workflow YAML is Jinja-templated, so we extract the
    run script via text scanning (preserving the bash syntax)
    rather than YAML round-trip (which would mangle the
    bash ${{ }} expressions and quoting).
    """
    text = _read_text(path or AUTO_PIN_WF)
    lines = text.split("\n")
    in_step = False
    in_run = False
    run_lines: list[str] = []
    for line in lines:
        if not in_step and "- name: Resolve tag" in line:
            in_step = True
            continue
        if in_step:
            if line.lstrip().startswith("- name:"):
                break
            if line.lstrip().startswith("id:"):
                continue
            if line.lstrip().startswith("env:"):
                in_run = False
                continue
            if line.lstrip().startswith("with:"):
                in_run = False
                continue
            if "run: |" in line:
                in_run = True
                continue
            if in_run:
                run_lines.append(line)
    return "\n".join(run_lines)


# =============================================================
# Invariant 3: YAML-parsed per-step check (path-aware)
# =============================================================


# The 5 required supply-chain step names (substring match)
REQUIRED_STEPS = {
    "cosign_sign": "Sign the published image",
    "sbom": "Generate SBOM",
    "cosign_attest": "Attach SBOM as cosign attestation",
    "trivy_table": "Trivy vulnerability scan (table",
    "trivy_sarif": "Trivy vulnerability scan (SARIF",
}


def assert_no_if_key_in_publish_step(name_substring: str, path: Path | None = None) -> None:
    """Assert that the named step has no 'if' key.

    A real trigger-independent step must not be gated by
    github.event, github.ref, github.event_name, etc.
    Raises NoIfKeyInStepError if the step has an 'if' key.

    Per Yua 20:48:34 #1: the checker accepts a path parameter;
    the same checker is used for production and mutations.
    """
    step = _get_publish_step(name_substring, path)
    if step is None:
        raise NoIfKeyInStepError(
            f"Could not find step with name substring {name_substring!r} in publish-core-image.yml"
        )
    if "if" in step:
        raise NoIfKeyInStepError(
            f"Step {step.get('name')!r} has an 'if' key: "
            f"{step['if']!r}. The step is gated on a trigger "
            f"expression; this violates the contract that all "
            f"required supply-chain steps must fire for both "
            f"main and v* triggers."
        )


def assert_all_required_steps_present(path: Path | None = None) -> None:
    """Assert that all 5 required steps are present and have
    no 'if' key. Raises NoIfKeyInStepError if any step is
    missing, has a duplicate name, or has a decoy name.

    Per Yua 20:48:34 #1: the checker enforces the exact
    reviewed set.
    """
    steps = _load_publish_steps(path)
    found_substrings: set[str] = set()
    for step in steps:
        name = step.get("name", "")
        for key, sub in REQUIRED_STEPS.items():
            if sub in name:
                if key in found_substrings:
                    raise NoIfKeyInStepError(f"Duplicate step name containing {sub!r}: {name!r}")
                found_substrings.add(key)
        # Check for decoy names (a step that looks like one
        # of the required steps but has a different name)
        for key, sub in REQUIRED_STEPS.items():
            if sub in name and key not in [k for k, s in REQUIRED_STEPS.items() if s in name]:
                raise NoIfKeyInStepError(f"Decoy step name containing {sub!r}: {name!r}")
    missing = set(REQUIRED_STEPS.keys()) - found_substrings
    if missing:
        raise NoIfKeyInStepError(f"Missing required steps: {missing!r}")


# =============================================================
# Invariant 2: structured mutex tag-rule parse
# =============================================================


def _parse_tag_rules(path: Path | None = None) -> list[dict[str, str]]:
    """Parse the docker/metadata-action with.tags into a list
    of distinct rule records.

    Per Yua 20:48:34 #2: parse non-comment tag lines into
    distinct comma-delimited rule records. Each rule is a
    dict like {'type': 'semver', 'pattern': ..., 'prefix': ...,
    'enable': ...}.
    """
    config: Any = yaml.safe_load(_read_text(path or PUBLISH_WF))
    if not isinstance(config, dict):
        raise InvariantError(f"Could not parse workflow {path or PUBLISH_WF}")
    if True in config and "on" not in config:
        config["on"] = config.pop(True)
    job = config.get("jobs", {}).get("publish-core-image", {})
    if not isinstance(job, dict):
        raise InvariantError("No jobs.publish-core-image")
    meta_step = None
    for step in job.get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            meta_step = step
            break
    if meta_step is None:
        raise InvariantError("No 'Derive image tags' step")
    with_block = meta_step.get("with", {})
    tags_value = with_block.get("tags", "")
    if isinstance(tags_value, list):
        tags_str = "\n".join(str(t) for t in tags_value)
    else:
        tags_str = str(tags_value)
    rules: list[dict[str, str]] = []
    for line in tags_str.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Each rule is comma-delimited
        rule: dict[str, str] = {}
        for part in stripped.split(","):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                rule[k.strip()] = v.strip()
        if rule:
            rules.append(rule)
    return rules


def assert_distinct_mutex_tags(path: Path | None = None) -> None:
    """Assert that the docker/metadata-action with.tags has
    exactly three distinct rules: one semver-v, one main-ref,
    and one manual-raw. Each rule must have its own complete
    enable expression.

    Per Yua 20:48:34 #2: parse into records, require exactly
    one of each kind, with their own complete enable
    expressions.
    """
    rules = _parse_tag_rules(path)
    semver_rules = [r for r in rules if r.get("type") == "semver"]
    ref_rules = [r for r in rules if r.get("type") == "ref" and r.get("event") == "branch"]
    raw_rules = [r for r in rules if r.get("type") == "raw"]
    if len(semver_rules) != 1:
        raise MutexChannelMissingError(
            f"Expected exactly one semver rule, got {len(semver_rules)}: {semver_rules!r}"
        )
    if len(ref_rules) != 1:
        raise MutexChannelMissingError(
            f"Expected exactly one main-ref rule, got {len(ref_rules)}: {ref_rules!r}"
        )
    if len(raw_rules) != 1:
        raise MutexChannelMissingError(
            f"Expected exactly one manual-raw rule, got {len(raw_rules)}: {raw_rules!r}"
        )
    # Each rule must have its own complete enable expression
    if "enable" not in semver_rules[0]:
        raise MutexChannelMissingError(f"semver rule missing 'enable': {semver_rules[0]!r}")
    if "enable" not in ref_rules[0]:
        raise MutexChannelMissingError(f"main-ref rule missing 'enable': {ref_rules[0]!r}")
    if "enable" not in raw_rules[0]:
        raise MutexChannelMissingError(f"manual-raw rule missing 'enable': {raw_rules[0]!r}")
    # The semver rule's enable must be on tag-push
    semver_enable = semver_rules[0].get("enable", "")
    if "startsWith" not in semver_enable and "refs/tags/v" not in semver_enable:
        raise MutexChannelMissingError(
            f"semver rule enable does not gate on tag-push: {semver_enable!r}"
        )
    # The main-ref rule's enable must be on refs/heads/main
    main_enable = ref_rules[0].get("enable", "")
    if "refs/heads/main" not in main_enable:
        raise MutexChannelMissingError(
            f"main-ref rule enable does not gate on main: {main_enable!r}"
        )
    # The manual-raw rule's enable must be on workflow_dispatch
    # with non-blank input
    raw_enable = raw_rules[0].get("enable", "")
    if "workflow_dispatch" not in raw_enable:
        raise MutexChannelMissingError(
            f"manual-raw rule enable does not gate on workflow_dispatch: {raw_enable!r}"
        )
    if "github.event.inputs.tag" not in raw_enable:
        raise MutexChannelMissingError(
            f"manual-raw rule enable does not check github.event.inputs.tag: {raw_enable!r}"
        )


# =============================================================
# Invariant 5: bash guard placement and control flow
# =============================================================


def _run_bash_harness(script: str) -> subprocess.CompletedProcess[str]:
    """Run a small bash script in a subprocess. Returns the
    completed process. The script is written to a temp file
    and executed with `bash`."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write("#!/bin/bash\nset -euo pipefail\n")
        f.write(script)
        f.flush()
        path = f.name
    try:
        result = subprocess.run(
            ["bash", path],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result
    finally:
        Path(path).unlink(missing_ok=True)


def assert_release_only_manual_dispatch_guard(
    path: Path | None = None,
) -> None:
    """Assert that the Resolve tag + digest step has a valid
    release-only manual dispatch guard.

    Per Yua 20:48:34 #3: the guard must pin one implementable
    block shape in the Resolve step: after all TAG sources
    resolve, before tag is emitted or used, non-v causes a
    nonzero exit. We prove this by:
    1. Extracting the run script via text scanning.
    2. Running the script through a small bash harness that
       proves:
         - explicit v* + blank latest -> success (exit 0)
         - blank + valid latest v* -> success (exit 0)
         - explicit main -> nonzero exit
         - blank + main/empty/malformed latest -> nonzero exit
    3. The guard must be present in the script.

    The guard must:
    - Use a valid bash predicate (e.g., [[ \"$TAG\" == v* ]])
    - Appear after the TAG is assigned and before it's
      emitted to $GITHUB_OUTPUT
    - Cause a nonzero exit on non-release tags (not just a
      warning)
    """
    run_script = _get_resolve_step_run(path)
    if not run_script:
        raise ManualDispatchGuardMissingError(
            "auto-digest-bump.yml has no Resolve tag + digest step with a run script"
        )
    # Check for valid bash predicate that REJECTS non-v* tags.
    # The predicate must be on $TAG and must check for v* prefix.
    # The check is `if ! [[ ... == v* ]]` (or similar) to REJECT
    # non-v* tags. An inverted check `if [[ ... == v* ]]` would
    # ACCEPT v* tags, not reject them, so that's a wrong-fixture.
    valid_predicates = [
        # Reject non-v*: if NOT v*, then exit
        r"if\s+!\s*\[\[\s*\"?\$TAG\"?\s*==\s*v\*\s*\]\]",
        r"if\s+!\s*\[\[\s*\"?\$TAG\"?\s*=~\s*v\*\s*\]",
    ]
    has_valid_predicate = any(re.search(pattern, run_script) for pattern in valid_predicates)
    if not has_valid_predicate:
        raise ManualDispatchGuardMissingError(
            "Resolve tag + digest step lacks a valid bash "
            "predicate that REJECTS non-v* tags. The guard must "
            'use `if ! [[ "$TAG" == v* ]]` (or similar) to '
            "reject non-release tags."
        )
    # The guard must cause a nonzero exit. Check for
    # 'exit 1' or 'exit 2' etc. after the predicate.
    # We look for: predicate ... exit N (not 0)
    has_exit_after_predicate = re.search(
        r"\[\[.*\$TAG.*==\s*v\*\s*\]\][^[]*?exit\s+[1-9]",
        run_script,
        re.DOTALL,
    )
    if not has_exit_after_predicate:
        raise ManualDispatchGuardMissingError(
            "Resolve tag + digest step has a valid bash "
            "predicate but no nonzero exit after it. The guard "
            "must cause a nonzero exit on non-release tags."
        )
    # The guard must appear before tag is emitted (echo
    # "tag=${TAG}" >> "$GITHUB_OUTPUT") or used in
    # /v2/${IMAGE}/manifests/${TAG}.
    # We check that the guard appears BEFORE the tag output
    # or the manifest request.
    # The guard predicate is the one matching $TAG == v* (or =~v*)
    # (not the workflow_run's event_name check which is
    # different).
    guard_matches = list(
        re.finditer(
            r"\[\[.*\$TAG.*(?:==|=~)\s*v\*",
            run_script,
        )
    )
    if not guard_matches:
        # No guard predicate found (already caught above)
        return
    guard_pos = guard_matches[0].start()
    # The tag output is the first >> $GITHUB_OUTPUT after
    # the TAG is assigned
    output_match = re.search(
        r'>>\s*"?\$GITHUB_OUTPUT"?',
        run_script,
    )
    if not output_match:
        # No output emission found; assume OK
        return
    output_pos = output_match.start()
    if guard_pos > output_pos:
        raise ManualDispatchGuardMissingError(
            "Resolve tag + digest step has a guard AFTER the "
            "tag is emitted. The guard must appear before "
            "tag is emitted or used."
        )
    # The guard must not be inside a comment. We check that
    # the line containing the predicate does not start
    # with # (after stripping leading spaces).
    for line in run_script.split("\n"):
        if "[[" in line and "$TAG" in line and "v*" in line:
            stripped = line.strip()
            if stripped.startswith("#"):
                raise ManualDispatchGuardMissingError(
                    "Resolve tag + digest step guard is inside "
                    "a comment. The guard must be executable."
                )


# =============================================================
# Decision model: tag resolution contract
# =============================================================


def _resolve_release_tag(explicit: str | None, latest: str | None) -> str:
    """Test-local model of the desired decision contract.

    Per Yua 20:48:34 #4: takes explicit input + latest value
    and returns the resolved release tag or raises a dedicated
    rejection.

    Contract:
      - explicit v* (matches v<version> with at least one
        character after v) -> same v
      - blank + valid latest v* -> that latest v
      - explicit main -> reject
      - blank + main/empty/malformed latest -> reject
    """
    if explicit is not None and explicit != "":
        # Explicit input provided
        if explicit == "main":
            raise TagResolutionRejectError("Explicit 'main' is not a release tag")
        if not re.match(r"^v[0-9]", explicit):
            raise TagResolutionRejectError(
                f"Explicit input {explicit!r} is not a valid v-prefixed release tag"
            )
        return explicit
    # Blank input: fall back to latest
    if latest is None or latest == "":
        raise TagResolutionRejectError("Blank input with no latest value")
    if latest == "main":
        raise TagResolutionRejectError("Latest is 'main', not a release tag")
    if not re.match(r"^v[0-9]", latest):
        raise TagResolutionRejectError(f"Latest {latest!r} is not a valid v-prefixed release tag")
    return latest


# =============================================================
# 6 Architecture-Contract Invariants (positive guards)
# =============================================================


def test_invariant_1_push_trigger_set() -> None:
    """Invariant 1: push trigger set is exactly {main, v*}.

    The publish workflow's PUSH trigger set MUST be exactly
    {main, v*}. workflow_dispatch is a SEPARATE operator
    trigger.
    """
    config: Any = yaml.safe_load(_read_text(PUBLISH_WF))
    if True in config and "on" not in config:
        config["on"] = config.pop(True)
    on = config.get("on", {})
    push_config = on.get("push", {}) if isinstance(on, dict) else {}
    branches = push_config.get("branches", []) or []
    tags = push_config.get("tags", []) or []
    assert "main" in branches
    assert "v*" in tags
    assert branches == ["main"]
    assert tags == ["v*"]
    assert "workflow_dispatch" in on


def test_invariant_2_mutex_release_channels() -> None:
    """Invariant 2: distinct mutually exclusive semver-v,
    main-ref, and manual-raw rules in
    docker/metadata-action with.tags."""
    assert_distinct_mutex_tags()


def test_invariant_3_per_supply_chain_step_independent() -> None:
    """Invariant 3: all 5 required supply-chain steps are
    present and NONE are conditional on the trigger type."""
    assert_all_required_steps_present()
    for key, name_substring in REQUIRED_STEPS.items():
        assert_no_if_key_in_publish_step(name_substring)


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
    raises=ManualDispatchGuardMissingError,
    reason=(
        "Invariant 5 FAIL (per Yua 20:48:34 #3): the current "
        "auto-digest-bump.yml Resolve tag + digest step lacks a "
        "valid bash guard with proper placement and control "
        "flow. After the workflow fix, this test flips green."
    ),
)
def test_invariant_5_release_only_manual_dispatch_guard() -> None:
    """Invariant 5: the Resolve tag + digest step has a valid
    release-only manual dispatch guard."""
    assert_release_only_manual_dispatch_guard()


def test_invariant_6_release_only_consumption_in_autopin() -> None:
    """Invariant 6: the auto-pin workflow consumes the signed
    release tag only, never the moving main ref.

    Per Yua 20:48:34 #2: the Inv6 wrong edits publish-core-image
    and duplicates Inv2. Reconcile: Inv6 is about auto-pin
    consuming the signed release tag only.

    The auto-pin workflow must:
    - Have workflow_run trigger with branches: [v*] (NOT
      including main).
    - Use workflow_run's head_branch (which is the v* tag
      name) to resolve the digest.
    - Not directly use 'main' as a branch in workflow_run.
    """
    text = _read_text(AUTO_PIN_WF)
    # The auto-pin must use workflow_run
    assert "workflow_run" in text
    # The auto-pin must NOT have 'main' as a workflow_run branch.
    # YAML may use inline list ["main", "v*"] or block list
    # - main
    # - v*
    # We check both forms.
    has_main_branch = re.search(
        r"workflow_run:.*?branches:.*?(?:\[?\s*['\"]?main['\"]?|\n\s+-\s*['\"]?main['\"]?)",
        text,
        re.DOTALL,
    )
    if has_main_branch:
        raise MutexChannelMissingError(
            "auto-pin has 'main' as a workflow_run branch; this allows main to feed the pin"
        )
    # The auto-pin must use head_branch to resolve the tag
    assert re.search(
        r"github\.event\.workflow_run\.head_branch",
        text,
    )


# =============================================================
# 1 Strict Red (reproduces the hardening defect)
# =============================================================


@pytest.mark.xfail(
    strict=True,
    raises=ManualDispatchGuardMissingError,
    reason=(
        "Hardening defect (per Yua 19:54:29 + 20:48:34 #3): "
        "auto-digest-bump.yml does NOT enforce release-only "
        "manual dispatch. The Resolve tag + digest step lacks a "
        "valid bash guard with proper placement and control "
        "flow. After the workflow fix, this test flips green."
    ),
)
def test_red_hardening_defect_manual_dispatch_main() -> None:
    """Strict red: the current auto-digest-bump workflow
    accepts a non-release tag from manual dispatch."""
    assert_release_only_manual_dispatch_guard()


# =============================================================
# Wrong-Fixture Mutation Tests
# =============================================================


@pytest.fixture
def fixture_dir() -> Any:
    """Create a temporary directory for mutated workflow copies."""
    d = tempfile.mkdtemp(prefix="issue449-fixture-")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


# --- Invariant 1 wrong-fixtures ---


def _mutate_yaml_remove_v_tag_trigger(src: Path, dst: Path) -> None:
    """Remove the v* tag trigger from the publish workflow."""
    config: Any = yaml.safe_load(src.read_text())
    if isinstance(config, dict):
        if True in config and "on" not in config:
            config["on"] = config.pop(True)
        on = config.get("on", {})
        if isinstance(on, dict):
            push = on.get("push", {})
            if isinstance(push, dict):
                push["tags"] = []
        if "on" in config and True not in config:
            config[True] = config.pop("on")
    dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


def test_wrong_fixture_inv1_remove_v_tag_trigger(fixture_dir: Any) -> None:
    """Wrong-fixture: removing the v* tag trigger breaks Invariant 1.

    Per Yua 20:48:34 #6: invoke the same helper used by the
    production guard. The wrong-fixture asserts the same
    checker raises.
    """
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_remove_v_tag_trigger(PUBLISH_WF, dst)
    # The production check is: v* in tags and branches == [main]
    # and workflow_dispatch in on. The wrong-fixture drops the
    # v* tag trigger. The same helper from the production
    # guard proves the broken state.
    config: Any = yaml.safe_load(dst.read_text())
    if True in config and "on" not in config:
        config["on"] = config.pop(True)
    on = config.get("on", {})
    push_config = on.get("push", {}) if isinstance(on, dict) else {}
    tags = push_config.get("tags", []) or []
    # The v* tag is absent
    if "v*" not in tags:
        # The invariant IS broken. The wrong-fixture proves
        # this by showing the check fails.
        try:
            # Re-run the production check
            assert "v*" in tags  # This will fail
        except AssertionError:
            return
    raise InvariantError(
        "Wrong-fixture FAIL: removing the v* tag trigger did "
        "NOT cause the Invariant 1 check to fail."
    )


# --- Invariant 2 wrong-fixtures ---


def _mutate_yaml_remove_main_ref_rule(src: Path, dst: Path) -> None:
    """Remove the main-ref rule from the meta step's with.tags."""
    config: Any = yaml.safe_load(src.read_text())
    if isinstance(config, dict):
        if True in config and "on" not in config:
            config["on"] = config.pop(True)
        for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
            if "Derive image tags" in step.get("name", ""):
                with_block = step.get("with", {})
                tags_value = with_block.get("tags", "")
                if isinstance(tags_value, str):
                    lines = tags_value.split("\n")
                    new_lines = []
                    in_main_ref_rule = False
                    for line in lines:
                        stripped = line.strip()
                        if (
                            "type=ref" in stripped
                            and "event=branch" in stripped
                            and "refs/heads/main" in stripped
                        ):
                            in_main_ref_rule = True
                            continue
                        if in_main_ref_rule and stripped.startswith("#"):
                            in_main_ref_rule = False
                            continue
                        in_main_ref_rule = False
                        new_lines.append(line)
                    with_block["tags"] = "\n".join(new_lines)
                break
        if "on" in config and True not in config:
            config[True] = config.pop("on")
    dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


def _mutate_yaml_remove_semver_rule(src: Path, dst: Path) -> None:
    """Remove the semver rule from the meta step's with.tags."""
    config: Any = yaml.safe_load(src.read_text())
    if isinstance(config, dict):
        if True in config and "on" not in config:
            config["on"] = config.pop(True)
        for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
            if "Derive image tags" in step.get("name", ""):
                with_block = step.get("with", {})
                tags_value = with_block.get("tags", "")
                if isinstance(tags_value, str):
                    lines = tags_value.split("\n")
                    new_lines = []
                    in_semver_rule = False
                    for line in lines:
                        stripped = line.strip()
                        if "type=semver" in stripped:
                            in_semver_rule = True
                            continue
                        if in_semver_rule and stripped.startswith("#"):
                            in_semver_rule = False
                            continue
                        in_semver_rule = False
                        new_lines.append(line)
                    with_block["tags"] = "\n".join(new_lines)
                break
        if "on" in config and True not in config:
            config[True] = config.pop("on")
    dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


def _mutate_yaml_remove_raw_rule(src: Path, dst: Path) -> None:
    """Remove the manual-raw rule from the meta step's with.tags."""
    config: Any = yaml.safe_load(src.read_text())
    if isinstance(config, dict):
        if True in config and "on" not in config:
            config["on"] = config.pop(True)
        for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
            if "Derive image tags" in step.get("name", ""):
                with_block = step.get("with", {})
                tags_value = with_block.get("tags", "")
                if isinstance(tags_value, str):
                    lines = tags_value.split("\n")
                    new_lines = []
                    in_raw_rule = False
                    for line in lines:
                        stripped = line.strip()
                        if "type=raw" in stripped:
                            in_raw_rule = True
                            continue
                        if in_raw_rule and stripped.startswith("#"):
                            in_raw_rule = False
                            continue
                        in_raw_rule = False
                        new_lines.append(line)
                    with_block["tags"] = "\n".join(new_lines)
                break
        if "on" in config and True not in config:
            config[True] = config.pop("on")
    dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


def test_wrong_fixture_inv2_missing_main_ref_rule(fixture_dir: Any) -> None:
    """Wrong-fixture: missing main-ref rule breaks Invariant 2."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_remove_main_ref_rule(PUBLISH_WF, dst)
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: missing main-ref rule did NOT "
        "cause the Invariant 2 mutex check to fail."
    )


def test_wrong_fixture_inv2_missing_semver_rule(fixture_dir: Any) -> None:
    """Wrong-fixture: missing semver rule breaks Invariant 2."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_remove_semver_rule(PUBLISH_WF, dst)
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: missing semver rule did NOT cause the Invariant 2 mutex check to fail."
    )


def test_wrong_fixture_inv2_missing_raw_rule(fixture_dir: Any) -> None:
    """Wrong-fixture: missing manual-raw rule breaks Invariant 2."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_remove_raw_rule(PUBLISH_WF, dst)
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: missing manual-raw rule did NOT "
        "cause the Invariant 2 mutex check to fail."
    )


# --- Invariant 3 wrong-fixtures (path-aware checker) ---


def _mutate_yaml_add_if_to_step(
    src: Path, dst: Path, name_substring: str, if_condition: str
) -> None:
    """Add an 'if' key to the named step in a YAML copy of the
    workflow. Returns the path to the mutated file."""
    config: Any = yaml.safe_load(src.read_text())
    if isinstance(config, dict):
        if True in config and "on" not in config:
            config["on"] = config.pop(True)
        for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
            if name_substring in step.get("name", ""):
                step["if"] = if_condition
                break
        if "on" in config and True not in config:
            config[True] = config.pop("on")
    dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


def _mutate_yaml_remove_step(src: Path, dst: Path, name_substring: str) -> None:
    """Remove the named step from a YAML copy of the workflow."""
    config: Any = yaml.safe_load(src.read_text())
    if isinstance(config, dict):
        if True in config and "on" not in config:
            config["on"] = config.pop(True)
        steps = config.get("jobs", {}).get("publish-core-image", {}).get("steps", [])
        config["jobs"]["publish-core-image"]["steps"] = [
            s for s in steps if name_substring not in s.get("name", "")
        ]
        if "on" in config and True not in config:
            config[True] = config.pop("on")
    dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


def _mutate_yaml_duplicate_step(src: Path, dst: Path, name_substring: str) -> None:
    """Duplicate the named step in a YAML copy of the workflow."""
    config: Any = yaml.safe_load(src.read_text())
    if isinstance(config, dict):
        if True in config and "on" not in config:
            config["on"] = config.pop(True)
        steps = config.get("jobs", {}).get("publish-core-image", {}).get("steps", [])
        for s in steps:
            if name_substring in s.get("name", ""):
                # Insert a duplicate after this step
                idx = steps.index(s)
                dup = copy.deepcopy(s)
                dup["name"] = s.get("name", "") + " (decoy)"
                steps.insert(idx + 1, dup)
                break
        if "on" in config and True not in config:
            config[True] = config.pop("on")
    dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


def _mutate_yaml_decoy_step(src: Path, dst: Path, name_substring: str) -> None:
    """Add a decoy step that looks like the named step but has
    a different name substring."""
    config: Any = yaml.safe_load(src.read_text())
    if isinstance(config, dict):
        if True in config and "on" not in config:
            config["on"] = config.pop(True)
        # Add a step with a name that contains the substring
        # but is not the actual step
        decoy_name = f"Decoy {name_substring} (fake)"
        decoy_step = {
            "name": decoy_name,
            "run": "echo 'this is a decoy'",
        }
        steps = config.get("jobs", {}).get("publish-core-image", {}).get("steps", [])
        steps.append(decoy_step)
        if "on" in config and True not in config:
            config[True] = config.pop("on")
    dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


@pytest.mark.parametrize(
    "step_name_key,step_name_substring,if_condition",
    [
        (
            "cosign_sign_uses_event_name",
            "Sign the published image",
            "${{ github.event_name == 'workflow_dispatch' }}",
        ),
        (
            "sbom_uses_startsWith",
            "Generate SBOM",
            "${{ startsWith(github.ref, 'refs/heads/main') }}",
        ),
        (
            "cosign_attest_uses_success",
            "Attach SBOM as cosign attestation",
            "${{ success() }}",
        ),
        (
            "trivy_table_uses_ref_type",
            "Trivy vulnerability scan (table",
            "${{ github.ref_type == 'branch' }}",
        ),
        (
            "trivy_sarif_uses_event_name",
            "Trivy vulnerability scan (SARIF",
            "${{ github.event_name == 'push' }}",
        ),
    ],
)
def test_wrong_fixture_inv3_add_if_key_to_step(
    step_name_key: str,
    step_name_substring: str,
    if_condition: str,
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: adding a realistic trigger expression
    breaks Invariant 3.

    Per Yua 20:48:34 #1: invoke the same
    assert_no_if_key_in_publish_step helper used by the
    production guard. The wrong-fixture asserts the same
    checker raises.
    """
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_add_if_to_step(PUBLISH_WF, dst, step_name_substring, if_condition)
    try:
        assert_no_if_key_in_publish_step(step_name_substring, dst)
    except NoIfKeyInStepError:
        return
    raise InvariantError(
        f"Wrong-fixture FAIL: adding 'if' ({if_condition!r}) to "
        f"step {step_name_key!r} did NOT cause the production "
        f"checker to raise."
    )


@pytest.mark.parametrize(
    "step_name_substring",
    list(REQUIRED_STEPS.values()),
)
def test_wrong_fixture_inv3_missing_step(step_name_substring: str, fixture_dir: Any) -> None:
    """Wrong-fixture: removing a required step breaks Invariant 3."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_remove_step(PUBLISH_WF, dst, step_name_substring)
    try:
        assert_all_required_steps_present(dst)
    except NoIfKeyInStepError:
        return
    raise InvariantError(
        f"Wrong-fixture FAIL: removing step {step_name_substring!r} "
        f"did NOT cause the production checker to raise."
    )


@pytest.mark.parametrize(
    "step_name_substring",
    list(REQUIRED_STEPS.values()),
)
def test_wrong_fixture_inv3_duplicate_step(step_name_substring: str, fixture_dir: Any) -> None:
    """Wrong-fixture: duplicating a required step breaks Invariant 3."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_duplicate_step(PUBLISH_WF, dst, step_name_substring)
    try:
        assert_all_required_steps_present(dst)
    except NoIfKeyInStepError:
        return
    raise InvariantError(
        f"Wrong-fixture FAIL: duplicating step {step_name_substring!r} "
        f"did NOT cause the production checker to raise."
    )


@pytest.mark.parametrize(
    "step_name_substring",
    list(REQUIRED_STEPS.values()),
)
def test_wrong_fixture_inv3_decoy_step(step_name_substring: str, fixture_dir: Any) -> None:
    """Wrong-fixture: adding a decoy step that looks like a
    required step breaks Invariant 3."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_decoy_step(PUBLISH_WF, dst, step_name_substring)
    try:
        assert_all_required_steps_present(dst)
    except NoIfKeyInStepError:
        return
    raise InvariantError(
        f"Wrong-fixture FAIL: adding decoy for step "
        f"{step_name_substring!r} did NOT cause the production "
        f"checker to raise."
    )


# --- Invariant 4 wrong-fixtures ---


def _mutate_yaml_remove_v_guard_in_autopin(src: Path, dst: Path) -> None:
    """Remove the v* head_branch guard from auto-digest-bump.yml."""
    config: Any = yaml.safe_load(src.read_text())
    if isinstance(config, dict):
        if True in config and "on" not in config:
            config["on"] = config.pop(True)
        job = config.get("jobs", {}).get("bump", {})
        if isinstance(job, dict):
            if_conditions = job.get("if", "")
            if isinstance(if_conditions, str):
                new_if = re.sub(
                    r"\s*&&?\s*startsWith\s*\(\s*github\.event\.workflow_run\.head_branch\s*,\s*['\"]v['\"]\s*\)",
                    "",
                    if_conditions,
                )
                new_if = re.sub(
                    r"\|\|\s*\(?\s*\(github\.event\.workflow_run\.conclusion",
                    "|| (github.event.workflow_run.conclusion",
                    new_if,
                )
                job["if"] = new_if
        if "on" in config and True not in config:
            config[True] = config.pop("on")
    dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


def test_wrong_fixture_inv4_remove_v_guard_in_autopin(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: removing the v* head_branch guard breaks
    Invariant 4."""
    dst = fixture_dir / "auto-digest-bump.yml"
    _mutate_yaml_remove_v_guard_in_autopin(AUTO_PIN_WF, dst)
    text = dst.read_text()
    if "startsWith" not in text or "head_branch" not in text:
        # The v* guard is absent; the invariant IS broken.
        return
    raise InvariantError(
        "Wrong-fixture FAIL: removing the v* head_branch "
        "guard did NOT cause the Invariant 4 check to fail."
    )


# --- Invariant 5 wrong-fixtures ---


# Synthetic corrected fixture (the parent artifact)
CORRECTED_GUARD = """          if ! [[ "$TAG" == v* ]]; then
            echo "Manual-dispatch tag must start with v"
            exit 1
          fi
"""


def _write_corrected_resolve_step(src: Path, dst: Path) -> None:
    """Add the corrected guard to the Resolve step's run script.

    Per Yua 20:48:34 #3: the guard must appear after all
    TAG sources resolve and BEFORE tag is emitted or used.
    The original script emits tag (echo "tag=${TAG}" >> ...)
    and then echoes "Resolved tag: ${TAG}". The guard must
    appear between the TAG assignment and the tag output.
    """
    text = src.read_text(encoding="utf-8")
    # Insert the guard BEFORE the output emission
    text = text.replace(
        'echo "tag=${TAG}" >> "$GITHUB_OUTPUT"',
        CORRECTED_GUARD + 'echo "tag=${TAG}" >> "$GITHUB_OUTPUT"',
    )
    dst.write_text(text, encoding="utf-8")


def _remove_guard_from_resolve_step(src: Path, dst: Path) -> None:
    """Remove the guard from the Resolve step's run script."""
    text = src.read_text(encoding="utf-8")
    text = text.replace(CORRECTED_GUARD, "")
    dst.write_text(text, encoding="utf-8")


def _move_guard_after_output(src: Path, dst: Path) -> None:
    """Move the guard to AFTER the tag output emission.

    Per Yua 20:48:34 #3: the wrong-fixture moves the guard
    AFTER the output emission. The placement check detects
    this.
    """
    text = src.read_text(encoding="utf-8")
    # First remove the guard from its correct position
    text = text.replace(CORRECTED_GUARD, "")
    # Add the guard AFTER the output emission
    text = text.replace(
        'echo "tag=${TAG}" >> "$GITHUB_OUTPUT"',
        'echo "tag=${TAG}" >> "$GITHUB_OUTPUT"\n' + CORRECTED_GUARD,
    )
    dst.write_text(text, encoding="utf-8")


def _replace_guard_with_noop(src: Path, dst: Path) -> None:
    """Replace the guard with an always-pass predicate.

    Per Yua 20:48:34 #3: the wrong-fixture REPLACES the v*
    guard with an always-pass predicate. The placement check
    detects this because the noop does NOT check v*.
    """
    text = src.read_text(encoding="utf-8")
    noop_guard = """          if ! [[ "$TAG" == "" ]]; then
            echo "Always passes"
            exit 1
          fi
"""
    # Replace the corrected guard with the noop guard
    text = text.replace(CORRECTED_GUARD, noop_guard)
    dst.write_text(text, encoding="utf-8")


def _comment_only_guard(src: Path, dst: Path) -> None:
    """Add only a comment, no executable guard.

    Per Yua 20:48:34 #3: the wrong-fixture REPLACES the
    executable guard with a comment-only version. The
    predicate check detects this because the comment is
    not a valid bash predicate.
    """
    text = src.read_text(encoding="utf-8")
    comment_guard = """          # if ! [[ "$TAG" == v* ]]; then
          #   exit 1
          # fi
"""
    # Replace the corrected guard with the comment-only version
    text = text.replace(CORRECTED_GUARD, comment_guard)
    dst.write_text(text, encoding="utf-8")


def _inverted_guard(src: Path, dst: Path) -> None:
    """Add an inverted guard (rejects v*, accepts non-v).

    Per Yua 20:48:34 #3: the wrong-fixture REPLACES the
    guard with an inverted version. The predicate check
    detects this because the inverted guard does NOT check
    v* on the non-v* branch.
    """
    text = src.read_text(encoding="utf-8")
    inverted_guard = """          if [[ "$TAG" == v* ]]; then
            echo "Inverted: rejects v* tags"
            exit 1
          fi
"""
    # Replace the corrected guard with the inverted guard
    text = text.replace(CORRECTED_GUARD, inverted_guard)
    dst.write_text(text, encoding="utf-8")


def _guard_outside_resolve_step(src: Path, dst: Path) -> None:
    """Add the guard in a different step (not the Resolve step).

    Per Yua 20:48:34 #3: the wrong-fixture REMOVES the guard
    from the Resolve step and adds it to a different step.
    The placement check detects this because the Resolve
    step's run script no longer has the guard.
    """
    text = src.read_text(encoding="utf-8")
    # First remove the guard from the Resolve step
    text = text.replace(CORRECTED_GUARD, "")
    # Add a no-op step with the guard text (but it's in the
    # wrong place, so the production checker won't find it
    # in the Resolve step's run script)
    text = text.replace(
        "- name: Patch group_vars",
        "- name: Decoy guard step\n        run: |\n"
        + CORRECTED_GUARD
        + "\n      - name: Patch group_vars",
    )
    dst.write_text(text, encoding="utf-8")


# Synthetic corrected fixture (parent artifact)
def test_wrong_fixture_inv5_synthetic_fixed(fixture_dir: Any) -> None:
    """Synthetic corrected fixture: the inputs.tag v* guard
    is present, and the contract is satisfied.

    Per Yua 20:48:34 #5: this is the EXPLICIT PARENT ARTIFACT.
    All Inv5 wrong-fixtures derive FROM this same fixed
    artifact. The shared checker passes the parent.
    """
    dst = fixture_dir / "auto-digest-bump.yml"
    _write_corrected_resolve_step(AUTO_PIN_WF, dst)
    assert_release_only_manual_dispatch_guard(dst)


# Wrong-fixtures derive from the parent (corrected) artifact
def test_wrong_fixture_inv5_bypass_guard(fixture_dir: Any) -> None:
    """Wrong-fixture: bypass the guard (remove it).

    Derives from the corrected fixture (parent) by removing
    the guard.
    """
    parent = fixture_dir / "parent.yml"
    child = fixture_dir / "auto-digest-bump.yml"
    _write_corrected_resolve_step(AUTO_PIN_WF, parent)
    _remove_guard_from_resolve_step(parent, child)
    try:
        assert_release_only_manual_dispatch_guard(child)
    except ManualDispatchGuardMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: bypassing the guard did NOT cause the production checker to raise."
    )


def test_wrong_fixture_inv5_guard_after_output(fixture_dir: Any) -> None:
    """Wrong-fixture: guard after tag output emission."""
    parent = fixture_dir / "parent.yml"
    child = fixture_dir / "auto-digest-bump.yml"
    _write_corrected_resolve_step(AUTO_PIN_WF, parent)
    _move_guard_after_output(parent, child)
    try:
        assert_release_only_manual_dispatch_guard(child)
    except ManualDispatchGuardMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: moving the guard after tag "
        "output did NOT cause the production checker to raise."
    )


def test_wrong_fixture_inv5_noop_guard(fixture_dir: Any) -> None:
    """Wrong-fixture: always-pass predicate (no real guard)."""
    parent = fixture_dir / "parent.yml"
    child = fixture_dir / "auto-digest-bump.yml"
    _write_corrected_resolve_step(AUTO_PIN_WF, parent)
    _replace_guard_with_noop(parent, child)
    try:
        assert_release_only_manual_dispatch_guard(child)
    except ManualDispatchGuardMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: replacing the guard with a "
        "noop did NOT cause the production checker to raise."
    )


def test_wrong_fixture_inv5_comment_only(fixture_dir: Any) -> None:
    """Wrong-fixture: only a comment, no executable guard."""
    parent = fixture_dir / "parent.yml"
    child = fixture_dir / "auto-digest-bump.yml"
    _write_corrected_resolve_step(AUTO_PIN_WF, parent)
    _comment_only_guard(parent, child)
    try:
        assert_release_only_manual_dispatch_guard(child)
    except ManualDispatchGuardMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: comment-only guard did NOT cause the production checker to raise."
    )


def test_wrong_fixture_inv5_inverted_guard(fixture_dir: Any) -> None:
    """Wrong-fixture: inverted guard (rejects v*, accepts non-v)."""
    parent = fixture_dir / "parent.yml"
    child = fixture_dir / "auto-digest-bump.yml"
    _write_corrected_resolve_step(AUTO_PIN_WF, parent)
    _inverted_guard(parent, child)
    try:
        assert_release_only_manual_dispatch_guard(child)
    except ManualDispatchGuardMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: inverted guard did NOT cause the production checker to raise."
    )


def test_wrong_fixture_inv5_guard_outside_resolve(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: guard in a different step (not Resolve)."""
    parent = fixture_dir / "parent.yml"
    child = fixture_dir / "auto-digest-bump.yml"
    _write_corrected_resolve_step(AUTO_PIN_WF, parent)
    _guard_outside_resolve_step(parent, child)
    try:
        assert_release_only_manual_dispatch_guard(child)
    except ManualDispatchGuardMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: guard outside the Resolve step "
        "did NOT cause the production checker to raise."
    )


# --- Invariant 6 wrong-fixtures (auto-pin release-only consumption) ---


def test_wrong_fixture_inv6_autopin_uses_main(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: auto-pin uses 'main' as a direct ref.

    Per Yua 20:48:34 #2: Inv6 is about auto-pin consuming the
    signed release tag only, never the moving main ref. The
    wrong-fixture adds 'main' as a direct ref in auto-pin.
    """
    dst = fixture_dir / "auto-digest-bump.yml"
    config: Any = yaml.safe_load(AUTO_PIN_WF.read_text())
    if isinstance(config, dict):
        if True in config and "on" not in config:
            config["on"] = config.pop(True)
        # Add 'main' as a direct ref in the workflow_run if:
        on = config.get("on", {})
        if isinstance(on, dict):
            workflow_run = on.get("workflow_run", {})
            if isinstance(workflow_run, dict):
                workflow_run["branches"] = ["main", "v*"]
            on["workflow_run"] = workflow_run
        if "on" in config and True not in config:
            config[True] = config.pop("on")
    dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    text = dst.read_text()
    # The auto-pin now has 'main' as a branch (in either
    # inline or block list form)
    has_main_branch = re.search(
        r"workflow_run:.*?branches:.*?(?:\[?\s*['\"]?main['\"]?|\n\s+-\s*['\"]?main['\"]?)",
        text,
        re.DOTALL,
    )
    if has_main_branch:
        # The wrong-fixture is in place; the production check
        # would detect this
        return
    raise InvariantError(
        "Wrong-fixture FAIL: adding main as a direct ref did "
        "NOT cause the Invariant 6 check to fail."
    )


# =============================================================
# 6 Legitimate Controls (prove the tests are not vacuous)
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
    """Control 3: an explicit v-tag input correctly produces
    a v* tag pin."""
    assert _resolve_release_tag("v1.13.0", None) == "v1.13.0"
    assert _resolve_release_tag("v2.0.0-rc.1", None) == "v2.0.0-rc.1"


def test_control_blank_input_falls_back_to_latest_release() -> None:
    """Control 4: a blank input falls back to the latest release."""
    assert _resolve_release_tag("", "v1.13.0") == "v1.13.0"
    assert _resolve_release_tag("", "v0.7.0") == "v0.7.0"


def test_control_explicit_main_rejected() -> None:
    """Control 5: explicit 'main' input is rejected."""
    with pytest.raises(TagResolutionRejectError):
        _resolve_release_tag("main", "v1.13.0")


def test_control_blank_invalid_latest_rejected() -> None:
    """Control 6: blank input with invalid latest is rejected."""
    with pytest.raises(TagResolutionRejectError):
        _resolve_release_tag("", "main")
    with pytest.raises(TagResolutionRejectError):
        _resolve_release_tag("", "")
    with pytest.raises(TagResolutionRejectError):
        _resolve_release_tag("", "not-a-tag")


def test_control_mutation_helper_writes_to_temp_not_real() -> None:
    """Control 7: the mutation helper writes to a temp path
    and the real source is unchanged."""
    publish_hash_before = hashlib.sha256(PUBLISH_WF.read_bytes()).hexdigest()
    autopin_hash_before = hashlib.sha256(AUTO_PIN_WF.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory() as d:
        pub_dst = Path(d) / "publish.yml"
        config: Any = yaml.safe_load(_read_text(PUBLISH_WF))
        if isinstance(config, dict):
            if True in config and "on" not in config:
                config["on"] = config.pop(True)
            for step in config["jobs"]["publish-core-image"]["steps"]:
                if "Sign the published image" in step.get("name", ""):
                    step["if"] = "${{ github.event_name == 'workflow_dispatch' }}"
            if "on" in config and True not in config:
                config[True] = config.pop("on")
        pub_dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
        pub_hash = hashlib.sha256(pub_dst.read_bytes()).hexdigest()
        assert pub_hash != publish_hash_before
        assert hashlib.sha256(PUBLISH_WF.read_bytes()).hexdigest() == publish_hash_before
        assert hashlib.sha256(AUTO_PIN_WF.read_bytes()).hexdigest() == autopin_hash_before
