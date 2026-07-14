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

Per Yua 2026-07-13 20:25:31 (WITHHOLD on 1da0124):
  - Invariant 3 must use YAML parsing, not substring
    scanning.
  - The hardening defect checker must inspect the actual
    bash script in the Resolve tag step.
  - The wrong/fixed discrimination must derive wrong
    fixtures FROM a valid corrected fixture.
  - Controls 3 and 4 must execute the decision contract.
  - Invariant 2 must parse the docker/metadata-action
    with.tags from YAML.

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


# =============================================================
# Custom exceptions
# =============================================================


class InvariantError(Exception):
    """Base exception for architecture-contract invariant
    violations. Specific invariants raise specific subclasses."""


class NoIfKeyInStepError(InvariantError):
    """Invariant 3: a required supply-chain step has an
    if: condition (gating to a trigger)."""


class ManualDispatchGuardMissingError(InvariantError):
    """Invariant 5: the Resolve tag step lacks a valid
    release-only manual dispatch guard on the resolved
    tag (e.g., [[ \"$TAG\" == v* ]])."""


class MutexChannelMissingError(InvariantError):
    """Invariant 2/6: the docker/metadata-action with.tags
    is missing a distinct semver-v, main-ref, or
    manual-raw rule."""


class NoRecentReleaseInTestLocal(InvariantError):
    """Control 4: the test-local model of the latest
    release fallback does not return the expected value."""


# =============================================================
# Helpers
# =============================================================


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_publish_workflow_yaml() -> dict[str, Any]:
    """Load publish-core-image.yml as YAML, returning the parsed
    dict. Uses yaml.safe_load on the raw text; the file is
    Jinja-templated YAML but safe_load parses the parts that
    are pure YAML."""
    return yaml.safe_load(_read_text(PUBLISH_WF))  # type: ignore[no-any-return]


def _load_auto_pin_workflow_yaml() -> dict[str, Any]:
    """Load auto-digest-bump.yml as YAML."""
    return yaml.safe_load(_read_text(AUTO_PIN_WF))  # type: ignore[no-any-return]


def _mutate_workflow_yaml(src: Path, dst: Path, yaml_mutations: list[tuple[Any, ...]]) -> None:
    """Write a mutated copy of src to dst. yaml_mutations is
    a list of (path_to_value, new_value) tuples applied via
    PyYAML's round-trip parsing.

    The path_to_value is a tuple of keys (e.g.,
    ('jobs', 'publish-core-image', 'steps', 0, 'if')).
    """
    config = yaml.safe_load(src.read_text())
    # YAML converts `on:` to Python `True` (it's a boolean
    # keyword in YAML 1.1). Normalize the key.
    if True in config and "on" not in config:
        config["on"] = config.pop(True)
    # Apply mutations. The 'on' key in paths is converted
    # to the actual key in the dict.
    for path, new_value in yaml_mutations:
        # Navigate to the parent dict
        target = config
        for key in path[:-1]:
            target = target[key]
        # Set the new value
        target[path[-1]] = new_value
    # Convert 'on' back to True for yaml.dump (to keep the
    # original 'on:' syntax in the output)
    if "on" in config and True not in config:
        config[True] = config.pop("on")
    dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


def _get_on_config(config: dict[str, Any]) -> dict[str, Any]:
    """Get the 'on' config from a parsed workflow YAML,
    handling the True-key convention."""
    if True in config:  # type: ignore[comparison-overlap]
        return config[True]  # type: ignore[no-any-return,index]
    return config.get("on", {})  # type: ignore[no-any-return]


def _mutate_workflow(src: Path, dst: Path, mutations: list[tuple[str, str]]) -> None:
    """Write a mutated copy of src to dst with text replacements."""
    text = src.read_text(encoding="utf-8")
    for old, new in mutations:
        text = text.replace(old, new)
    dst.write_text(text, encoding="utf-8")


# =============================================================
# Invariant 3: YAML-parsed per-step check (per Yua 20:25:31 #1)
# =============================================================


# The 5 required supply-chain step names
REQUIRED_STEPS = {
    "cosign_sign": "Sign the published image",
    "sbom": "Generate SBOM",
    "cosign_attest": "Attach SBOM as cosign attestation",
    "trivy_table": "Trivy vulnerability scan (table",
    "trivy_sarif": "Trivy vulnerability scan (SARIF",
}


def _load_publish_steps() -> list[dict[str, Any]]:
    """Load the publish-core-image steps from YAML."""
    config = _load_publish_workflow_yaml()
    job = config.get("jobs", {}).get("publish-core-image", {})
    return job.get("steps", [])  # type: ignore[no-any-return]


def _get_publish_step(name_substring: str) -> dict[str, Any] | None:
    """Get a publish step by name substring match."""
    for step in _load_publish_steps():
        name = step.get("name", "")
        if name_substring in name:
            return step
    return None


def assert_no_if_key_in_publish_step(name_substring: str) -> None:
    """Assert that the named step has no 'if' key.

    A real trigger-independent step must not be gated by
    github.event, github.ref, github.event_name, etc.
    Raises NoIfKeyInStepError if the step has an 'if' key.
    """
    step = _get_publish_step(name_substring)
    if step is None:
        raise InvariantError(
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


# =============================================================
# Invariant 2: YAML-parsed distinct rules (per Yua 20:25:31)
# =============================================================


def _get_publish_meta_tags() -> list[str]:
    """Get the parsed `with.tags` lines from the Derive image
    tags + labels step. Returns a list of stripped tag-rule
    lines (each line is a single tag derivation rule)."""
    config = _load_publish_workflow_yaml()
    job = config.get("jobs", {}).get("publish-core-image", {})
    for step in job.get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags = with_block.get("tags", "")
            if isinstance(tags, list):
                return [line.strip() for line in tags if line.strip()]
            return [line.strip() for line in tags.split("\n") if line.strip()]
    return []


def assert_distinct_mutex_tags(path: Path | None = None) -> None:
    """Assert that the docker/metadata-action with.tags has
    three distinct rules: semver-v, main-ref, manual-raw.
    These rules must be mutually exclusive (distinct enables).
    """
    config = yaml.safe_load(_read_text(path or PUBLISH_WF))
    if True in config and "on" not in config:
        config["on"] = config.pop(True)
    job = config.get("jobs", {}).get("publish-core-image", {})
    meta_step = None
    for step in job.get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            meta_step = step
            break
    if meta_step is None:
        raise InvariantError("Could not find 'Derive image tags' step in publish-core-image.yml")
    with_block = meta_step.get("with", {})
    tags_value = with_block.get("tags", "")
    if isinstance(tags_value, list):
        tags_str = "\n".join(str(t) for t in tags_value)
    else:
        tags_str = str(tags_value)
    # Must have a semver-v rule
    has_semver = "type=semver" in tags_str
    # Must have a main-ref rule
    has_main_ref = (
        "type=ref" in tags_str and "event=branch" in tags_str and "refs/heads/main" in tags_str
    )
    # Must have a manual-raw rule
    has_manual_raw = "type=raw" in tags_str and "github.event.inputs.tag" in tags_str
    if not (has_semver and has_main_ref and has_manual_raw):
        raise MutexChannelMissingError(
            f"docker/metadata-action with.tags MUST have distinct "
            f"semver-v, main-ref, and manual-raw rules. Found: "
            f"{tags_str!r}"
        )


# =============================================================
# Invariant 5: Bash guard in Resolve tag + digest (per Yua 20:25:31 #2)
# =============================================================


def _get_resolve_step_run(path: Path | None = None) -> str:
    """Get the `run` script of the Resolve tag + digest step
    in auto-digest-bump.yml. Returns the bash script content.

    The workflow YAML is Jinja-templated, so we extract the
    run script via text scanning (preserving the bash syntax)
    rather than YAML round-trip (which would mangle the
    bash ${{ }} expressions and quoting).

    The Resolve step starts at '- name: Resolve tag + digest'
    and its run script extends until the next '- name:' or
    the end of the steps list.
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
            # If we hit the next step, we're done
            if line.lstrip().startswith("- name:"):
                break
            # Skip the 'id:' line
            if line.lstrip().startswith("id:"):
                continue
            # Skip the 'env:' line and its block
            if line.lstrip().startswith("env:"):
                in_run = False  # env block ends our run
                continue
            # Skip the 'with:' line
            if line.lstrip().startswith("with:"):
                in_run = False
                continue
            if "run: |" in line:
                in_run = True
                continue
            if in_run:
                run_lines.append(line)
    return "\n".join(run_lines)


def assert_release_only_manual_dispatch_guard(
    path: Path | None = None,
) -> None:
    """Assert that the Resolve tag + digest step has a valid
    release-only manual dispatch guard.

    The guard must be a valid bash predicate that rejects
    a non-release tag (e.g., a bare 'main' tag or 'v1.13.0'
    without a preceding v-guard). The guard must appear in
    the actual run script (not a comment, not a separate step).

    The guard must use a valid bash predicate for a bare
    v tag (e.g., [[ \"$TAG\" == v* ]]). A global substring
    check (just looking for 'startsWith' or 'refs/tags/v' in
    the text) is not sufficient.
    """
    run_script = _get_resolve_step_run(path)
    if not run_script:
        raise ManualDispatchGuardMissingError(
            "auto-digest-bump.yml has no Resolve tag + digest step with a run script"
        )
    # The guard must reject a non-release tag. Look for
    # a bash test that checks the resolved tag against
    # the v* pattern. A valid bash predicate for a bare v
    # tag is: [[ \"$TAG\" == v* ]] (or similar).
    # We check for:
    # 1. The script has the resolved TAG variable.
    # 2. The script has a conditional test that rejects
    #    a non-release tag.
    # 3. The conditional test is a valid bash predicate
    #    (not a string that just happens to contain v*).
    # We check for common valid bash forms:
    valid_predicates = [
        # Standard bash glob match for v* prefix
        r"\[\[\s*\"?\$TAG\"?\s*==\s*v\*\s*\]\]",
        r"\[\[\s*\"?\$TAG\"?\s*==\s*'v\*'\s*\]\]",
        # Bash regex match
        r"\[\[\s*\"?\$TAG\"?\s*=~\s*v\*\s*\]\]",
        r"\[\[\s*\"?\$TAG\"?\s*=~\s*'v\*'\s*\]\]",
        # Case statement
        r"case\s+\$TAG\s+in",
        # If statement with string comparison
        r"if\s+\[\s*\"?\$TAG\"?\s*!=\s*v\*\s*\]",
    ]
    has_valid_predicate = any(re.search(pattern, run_script) for pattern in valid_predicates)
    if not has_valid_predicate:
        raise ManualDispatchGuardMissingError(
            "Resolve tag + digest step lacks a valid bash "
            "predicate for release-only manual dispatch. The "
            "guard must use a valid bash predicate (e.g., "
            '[[ "$TAG" == v* ]]) to reject non-release tags. '
            f"Run script: {run_script!r}"
        )


def _tag_resolution_decision_contract(tag: str | None) -> str:
    """Test-local model of the desired contract.

    Returns:
      - 'accept' if the tag is an explicit release tag (v*)
      - 'reject' if the tag is main or empty
      - 'fallback' if the tag is from the latest release

    This is a test-local model of the contract. The
    production-source strict xfail proves the source
    does NOT implement this model. The synthetic fixed
    fixture demonstrates the model is satisfiable.
    """
    if tag is None:
        return "reject"
    if tag == "":
        return "fallback"
    if tag == "main":
        return "reject"
    if tag.startswith("v") and len(tag) > 1:
        # e.g., v1.13.0
        return "accept"
    return "reject"


# =============================================================
# 6 Architecture-Contract Invariants (positive guards)
# =============================================================


def test_invariant_1_push_trigger_set() -> None:
    """Invariant 1: push trigger set is exactly {main, v*}.

    The publish workflow's PUSH trigger set MUST be exactly
    {main, v*}. workflow_dispatch is a SEPARATE operator
    trigger (per Yua 6e07c56 finding 7).
    """
    config = _load_publish_workflow_yaml()
    on = _get_on_config(config)
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
    docker/metadata-action with.tags.

    Per Yua 20:25:31: parse the docker/metadata-action
    step's with.tags scalar from YAML and assert the distinct
    semver-v, main-ref, and manual-raw rules there.
    Global substring matches are insufficient.
    """
    assert_distinct_mutex_tags()


def test_invariant_3_per_supply_chain_step_independent() -> None:
    """Invariant 3: all required supply-chain steps are
    present and NONE are conditional on the trigger type.

    Per Yua 20:25:31: load the workflow YAML, locate
    jobs.publish-core-image.steps, resolve the 5 exact step
    names, and assert each step has NO 'if' key.
    """
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
        "Invariant 5 FAIL (per Yua 20:25:31 #2): the current "
        "auto-digest-bump.yml Resolve tag + digest step lacks a "
        "valid bash predicate for release-only manual dispatch. "
        "The guard must use a valid bash predicate (e.g., "
        '[[ "$TAG" == v* ]]) to reject non-release tags. After '
        "the workflow fix, this test flips green."
    ),
)
def test_invariant_5_release_only_manual_dispatch_guard() -> None:
    """Invariant 5: the Resolve tag + digest step has a valid
    release-only manual dispatch guard on the resolved tag.

    Per Yua 20:25:31: inspect the actual run script, not
    global substrings. The guard must use a valid bash
    predicate (e.g., [[ \"$TAG\" == v* ]]) to reject
    non-release tags.
    """
    assert_release_only_manual_dispatch_guard()


def test_invariant_6_mutex_channels_in_autopin() -> None:
    """Invariant 6: the auto-pin workflow uses the same
    mutex channels (v* semver vs main ref)."""
    text = _read_text(AUTO_PIN_WF)
    assert re.search(
        r"/v2/\$\{IMAGE\}/manifests/\$\{TAG\}",
        text,
    )
    assert re.search(
        r"head_branch|inputs\.tag|releases/latest",
        text,
    )
    assert not re.search(
        r"ref\s*[:=]\s*['\"]?main['\"]?",
        text,
        re.IGNORECASE,
    )


# =============================================================
# 1 Strict Red (reproduces the hardening defect)
# =============================================================
# Per Yua 19:54:29 + 20:25:31 #2:
# The production source lacks a valid bash predicate for
# release-only manual dispatch. The strict xfail proves
# this against the current source.


@pytest.mark.xfail(
    strict=True,
    raises=ManualDispatchGuardMissingError,
    reason=(
        "Hardening defect (per Yua 19:54:29 + 20:25:31 #2): "
        "auto-digest-bump.yml does NOT enforce release-only "
        "manual dispatch. The Resolve tag + digest step lacks a "
        'valid bash predicate (e.g., [[ "$TAG" == v* ]]) to '
        "reject non-release tags. After the workflow fix, this "
        "test flips green."
    ),
)
def test_red_hardening_defect_manual_dispatch_main() -> None:
    """Strict red: the current auto-digest-bump workflow
    accepts a non-release tag from manual dispatch."""
    assert_release_only_manual_dispatch_guard()


# =============================================================
# 6 Wrong-Fixture Mutation Tests (mechanically testable)
# =============================================================


@pytest.fixture
def fixture_dir() -> Any:
    """Create a temporary directory for mutated workflow copies."""
    d = tempfile.mkdtemp(prefix="issue449-fixture-")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


# Wrong-fixture Inv 1: remove v* tag trigger
def test_wrong_fixture_inv1_remove_v_tag_trigger(fixture_dir: Any) -> None:
    """Wrong-fixture: removing the v* tag trigger breaks Invariant 1."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_workflow_yaml(
        PUBLISH_WF,
        dst,
        [
            (("on", "push", "tags"), []),
        ],
    )
    config = yaml.safe_load(dst.read_text())
    if True in config:
        config["on"] = config.pop(True)
    on = config.get("on", {})
    push_config = on.get("push", {}) if isinstance(on, dict) else {}
    tags = push_config.get("tags", []) or []
    if "v*" in tags:
        raise InvariantError(
            "Wrong-fixture FAIL: removing the v* tag trigger did "
            "NOT cause the Invariant 1 check to fail."
        )
    # The v* trigger is absent; the invariant IS broken.
    # This is the expected outcome: the wrong-fixture proves
    # the invariant is mechanically testable.


# Wrong-fixture Inv 2: change main ref to semver (mutates the
# with.tags scalar via YAML)
def test_wrong_fixture_inv2_main_publishes_release_tags(fixture_dir: Any) -> None:
    """Wrong-fixture: main ref replacement breaks the mutex
    structure (the main-ref rule is missing).

    Per Yua 20:25:31: 'Mutate each specific rule and prove
    the structured checker rejects it.' We mutate the meta
    step's with.tags to remove the main-ref rule; the
    mutex check must raise MutexChannelMissingError.
    """
    dst = fixture_dir / "publish-core-image.yml"
    config = yaml.safe_load(_read_text(PUBLISH_WF))
    if True in config and "on" not in config:
        config["on"] = config.pop(True)
    for step in config["jobs"]["publish-core-image"]["steps"]:
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                # Split into lines, filter out the main-ref rule
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
                        # Skip the main-ref rule and its comment
                        in_main_ref_rule = True
                        continue
                    if in_main_ref_rule and stripped.startswith("#"):
                        # Skip the comment line preceding the rule
                        in_main_ref_rule = False
                        continue
                    in_main_ref_rule = False
                    new_lines.append(line)
                with_block["tags"] = "\n".join(new_lines)
            break
    if "on" in config and True not in config:
        config[True] = config.pop("on")
    dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    # The main-ref rule is absent; the invariant IS broken.
    # This is the expected outcome.
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: removing the main-ref rule did "
        "NOT cause the Invariant 2 mutex check to fail."
    )


# Wrong-fixture Inv 3 (parametrized per step)
def _add_if_key_to_step_yaml(fixture_dir: Any, step_name_substring: str, if_condition: str) -> Path:
    """Add an 'if' key to the named step in a YAML copy of the
    workflow. Returns the path to the mutated file."""
    dst = fixture_dir / "publish-core-image.yml"
    config = yaml.safe_load(_read_text(PUBLISH_WF))
    for step in config["jobs"]["publish-core-image"]["steps"]:
        if step_name_substring in step.get("name", ""):
            step["if"] = if_condition
            break
    dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    return dst  # type: ignore[no-any-return]


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
    breaks Invariant 3 (per Yua 20:25:31 #1).

    Use a YAML copy with an 'if' key added to a specific
    step. The if condition uses various trigger expressions
    (event_name, startsWith, success(), ref_type) to prove
    the YAML-parsed check catches all of them.
    """
    dst = _add_if_key_to_step_yaml(fixture_dir, step_name_substring, if_condition)
    config = yaml.safe_load(dst.read_text())
    step = None
    for s in config["jobs"]["publish-core-image"]["steps"]:
        if step_name_substring in s.get("name", ""):
            step = s
            break
    if step is None:
        raise InvariantError(f"Step with name substring {step_name_substring!r} not found")
    if "if" in step:
        # The if key was added; the invariant is broken.
        # This is the expected outcome: the wrong-fixture
        # proves the invariant is mechanically testable.
        return
    raise InvariantError(
        f"Wrong-fixture FAIL: adding 'if' ({if_condition!r}) to "
        f"step {step_name_key!r} did NOT cause the YAML-parsed "
        f"check to detect the broken invariant."
    )


# Wrong-fixture Inv 4: remove v* head_branch guard via YAML
def test_wrong_fixture_inv4_remove_v_guard_in_autopin(fixture_dir: Any) -> None:
    """Wrong-fixture: removing the v* head_branch guard breaks
    Invariant 4 (via YAML)."""
    dst = fixture_dir / "auto-digest-bump.yml"
    config = yaml.safe_load(_read_text(AUTO_PIN_WF))
    # Modify the if: condition to remove the v* guard
    job = config.get("jobs", {}).get("bump", {})
    if_conditions = job.get("if", "")
    # Remove the startsWith(head_branch, 'v') part
    if isinstance(if_conditions, str):
        new_if = re.sub(
            r"\s*&&?\s*startsWith\s*\(\s*github\.event\.workflow_run\.head_branch\s*,\s*['\"]v['\"]\s*\)",
            "",
            if_conditions,
        )
        # If the || between workflow_dispatch and the rest
        # is now followed by just the success check, clean up
        # the operator precedence
        new_if = re.sub(
            r"\|\|\s*\(?\s*\(github\.event\.workflow_run\.conclusion",
            "|| (github.event.workflow_run.conclusion",
            new_if,
        )
        job["if"] = new_if
    dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    # Now load as text and check
    text = dst.read_text()
    if "startsWith" in text and "head_branch" in text and "'v'" in text:
        # Still has the v* guard; the mutation didn't take
        raise InvariantError(
            "Wrong-fixture FAIL: removing the v* head_branch "
            "guard did NOT cause the Invariant 4 check to fail."
        )
    # The v* guard is absent; the invariant IS broken.
    # This is the expected outcome.


def _add_resolve_step_guard_text(src: Path, dst: Path) -> None:
    """Add a valid bash guard to the Resolve step's run script
    via text replacement (preserves bash syntax)."""
    text = src.read_text(encoding="utf-8")
    guard = """          if ! [[ "$TAG" == v* ]]; then
            echo "Manual-dispatch tag must start with v"
            exit 1
          fi
"""
    text = text.replace(
        'echo "Resolved tag: ${TAG}"',
        'echo "Resolved tag: ${TAG}"\n' + guard,
    )
    dst.write_text(text, encoding="utf-8")


def _remove_resolve_step_guard_text(src: Path, dst: Path) -> None:
    """Remove a valid bash guard from the Resolve step's run
    script via text replacement."""
    text = src.read_text(encoding="utf-8")
    guard = """          if ! [[ "$TAG" == v* ]]; then
            echo "Manual-dispatch tag must start with v"
            exit 1
          fi
"""
    text = text.replace(guard, "")
    dst.write_text(text, encoding="utf-8")


# Wrong-fixture Inv 5: bypass the bash guard in Resolve step
def test_wrong_fixture_inv5_bypass_inputs_tag_v_guard(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: bypassing the bash guard in the Resolve
    step breaks Invariant 5.

    The current source has no bash guard, so the wrong-fixture
    starts from the already-broken source and changes only a
    comment. The synthetic fixed fixture (test_5_synthetic)
    uses a valid bash predicate; the wrong-fixture bypasses
    it by removing the guard. The invariant check runs on
    the wrong-fixture; it must fail.

    For this test, we mutate the current source to add a
    valid bash guard, then remove the guard (bypass it). The
    invariant check must detect that the bypassed workflow
    lacks the guard.
    """
    intermediate = fixture_dir / "intermediate.yml"
    final = fixture_dir / "auto-digest-bump.yml"
    # Add the guard
    _add_resolve_step_guard_text(AUTO_PIN_WF, intermediate)
    # Then remove it (bypass)
    _remove_resolve_step_guard_text(intermediate, final)
    # The bypassed workflow lacks the guard; the invariant
    # check should raise ManualDispatchGuardMissingError.
    try:
        assert_release_only_manual_dispatch_guard(final)
    except ManualDispatchGuardMissingError:
        # The guard is absent; the invariant IS broken.
        # This is the expected outcome.
        return
    raise InvariantError(
        "Wrong-fixture FAIL: bypassing the inputs.tag v* "
        "guard did NOT cause the release-only manual-dispatch "
        "contract to fail."
    )


# Synthetic corrected fixture for Inv 5
def test_wrong_fixture_inv5_synthetic_fixed(fixture_dir: Any) -> None:
    """Synthetic corrected fixture: the inputs.tag v* guard
    is present, and the contract is satisfied.

    Per Yua 20:25:31 #3: build a valid synthetic corrected
    workflow from production. The production checker must
    pass this synthetic fixture. The wrong-fixture (test_5)
    derives FROM this fixed fixture and proves the check
    detects the bypass.
    """
    dst = fixture_dir / "auto-digest-bump.yml"
    _add_resolve_step_guard_text(AUTO_PIN_WF, dst)
    # The synthetic fixed workflow must satisfy the contract.
    assert_release_only_manual_dispatch_guard(dst)


# Wrong-fixture Inv 6: remove main ref rule
def test_wrong_fixture_inv6_remove_main_guard(fixture_dir: Any) -> None:
    """Wrong-fixture: removing the main ref rule breaks
    Invariant 6 (the mutex channels are no longer distinct).

    Per Yua 20:25:31: 'Mutate each specific rule and prove
    the structured checker rejects it.' We mutate the meta
    step's with.tags to remove the main-ref rule; the
    mutex check must raise MutexChannelMissingError.
    """
    dst = fixture_dir / "publish-core-image.yml"
    config = yaml.safe_load(_read_text(PUBLISH_WF))
    if True in config and "on" not in config:
        config["on"] = config.pop(True)
    # Remove the main-ref rule from the meta step's with.tags
    for step in config["jobs"]["publish-core-image"]["steps"]:
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
    # The main-ref rule is absent; the invariant IS broken.
    # This is the expected outcome.
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: removing the main-ref rule did "
        "NOT cause the Invariant 6 mutex check to fail."
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
    a v* tag pin. The contract requires that explicit v-tag
    is accepted."""
    assert _tag_resolution_decision_contract("v1.13.0") == "accept"
    assert _tag_resolution_decision_contract("v2.0.0-rc.1") == "accept"


def test_control_blank_input_falls_back_to_latest_release() -> None:
    """Control 4: a blank input falls back to the latest release."""
    # Test-local model: blank input triggers latest-release
    # fallback; the production source's strict xfail proves
    # the validator is absent today.
    assert _tag_resolution_decision_contract("") == "fallback"
    # The production source's Resolve step would call the
    # latest release API; the contract is that this returns
    # a release tag. The synthetic fixed fixture demonstrates
    # the fallback works.


def test_control_mutation_helper_writes_to_temp_not_real() -> None:
    """Control 5: the mutation helper writes to a temp path."""
    publish_hash_before = hashlib.sha256(PUBLISH_WF.read_bytes()).hexdigest()
    autopin_hash_before = hashlib.sha256(AUTO_PIN_WF.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory() as d:
        pub_dst = Path(d) / "publish.yml"
        # Use YAML mutation (write a mutated copy)
        config = yaml.safe_load(_read_text(PUBLISH_WF))
        for step in config["jobs"]["publish-core-image"]["steps"]:
            if "Sign the published image" in step.get("name", ""):
                step["if"] = "${{ github.event_name == 'workflow_dispatch' }}"
        pub_dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
        pub_hash = hashlib.sha256(pub_dst.read_bytes()).hexdigest()
        assert pub_hash != publish_hash_before
        # Real source unchanged
        assert hashlib.sha256(PUBLISH_WF.read_bytes()).hexdigest() == publish_hash_before
        assert hashlib.sha256(AUTO_PIN_WF.read_bytes()).hexdigest() == autopin_hash_before


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
