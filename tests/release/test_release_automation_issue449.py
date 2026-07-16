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

Per Yua 2026-07-13 20:57:08 (WITHHOLD on 6ea08a9):
  - Make the executable Bash proof actually run.
  - Fix the tag-rule parser to not split inside enable exprs.
  - Extract path-aware helpers for Inv1, Inv4, Inv6.
  - Inv6 is the channel-metadata rule / allowed-divergence
    contract, NOT a workflow_run.branches filter.
  - Inv3 exact-set proof needs full names + decoy logic.
  - Resolver must use real release tag grammar.
  - Slice doc and lock must be updated to match tests.

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


# Release tag grammar accepted by this repo (semver with v
# prefix, optional pre-release suffix, no build metadata).
# Mirrors what release-please emits and what the v* tag
# push trigger accepts.
# Project release tag grammar (single source).
# Per Yua 21:36:58 #1: one declared grammar that drives
# the Python resolver, the Bash regex, AND CORRECTED_GUARD.
#
# This is a DOCUMENTED BOUNDED SUBSET of SemVer 2.0.0
# with v prefix. It is NOT a full SemVer 2.0.0 implementation.
# The differences from full SemVer 2.0.0 are:
#
#   1. Build metadata (+build) is explicitly UNSUPPORTED.
#   2. Prerelease identifiers must be EITHER:
#      a. Pure numeric (no leading zeros): 0, 1, 42
#      b. Pure alphanumeric starting with a letter, may
#         include digits and internal hyphens: alpha,
#         rc-1, x-y-z
#      Mixed identifiers like "1x" or "01alpha" are
#      NOT supported (rarely used; document as bounded).
#   3. Core: non-zero-prefixed numbers (e.g., 1, 10; not 01).
#
# All other SemVer 2.0.0 rules apply. See
# https://semver.org/#spec-item-10 for the full spec.

# Building blocks (Python form uses non-capturing groups;
# Bash form uses capturing groups, derived by substitution).
_CORE_NUM = r"(?:0|[1-9][0-9]*)"
_CORE_3 = rf"{_CORE_NUM}\.{_CORE_NUM}\.{_CORE_NUM}"
_IDENT_NUM = r"(?:0|[1-9][0-9]*)"  # numeric, no leading zeros
_IDENT_ALNUM = r"(?:[a-zA-Z](?:[0-9a-zA-Z-]*[0-9a-zA-Z])?)"
# Alphanumeric: starts with letter, ends with alphanumeric
# (or is a single letter). May include internal hyphens.
_IDENT = rf"(?:{_IDENT_NUM}|{_IDENT_ALNUM})"
_PRERELEASE = rf"(?:-{_IDENT}(?:\.{_IDENT})*)?"
# Full project release grammar (Python form).
PROJECT_RELEASE_GRAMMAR_PYTHON = re.compile(rf"^v{_CORE_3}{_PRERELEASE}$")


def _python_to_bash(pattern: str) -> str:
    """Convert a Python regex to bash POSIX ERE.

    bash regex `=~` does not support (?:...) non-capturing
    groups; convert them to (...) capturing groups.
    """
    return pattern.replace("(?:", "(")


# Bash POSIX ERE equivalent (derived from Python form).
PROJECT_RELEASE_GRAMMAR_BASH = _python_to_bash(rf"^v{_CORE_3}{_PRERELEASE}$")
RELEASE_TAG_GRAMMAR = PROJECT_RELEASE_GRAMMAR_PYTHON
RELEASE_TAG_GRAMMAR_PYTHON = PROJECT_RELEASE_GRAMMAR_PYTHON
RELEASE_TAG_GRAMMAR_BASH = PROJECT_RELEASE_GRAMMAR_BASH


def _extract_guard_regex(guard: str) -> str:
    """Extract the regex from CORRECTED_GUARD.

    Per Yua 21:36:58 #1: derive/render the actual guard
    regex from the same grammar source.
    """
    m = re.search(r"\[\[\s+\"(\$TAG)\"\s+=~\s+(.*?)\s+\]\]", guard)
    if not m:
        raise ValueError(f"Could not extract guard regex: {guard!r}")
    return m.group(2)


def _run_bash_regex_test(regex: str, value: str) -> bool:
    """Test if the bash regex matches the value.

    Returns True if the value matches, False otherwise.
    """
    # Use single-quoted value to avoid shell expansion.
    escaped_value = value.replace("'", "'''")
    script = (
        "#!/bin/bash\n"
        f"TAG='{escaped_value}'\n"
        f'if [[ "$TAG" =~ {regex} ]]; then\n'
        '  echo "BASH_MATCH=1"\n'
        "else\n"
        '  echo "BASH_MATCH=0"\n'
        "fi\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        result = subprocess.run(
            ["bash", path],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return "BASH_MATCH=1" in result.stdout
    finally:
        Path(path).unlink(missing_ok=True)


# The cross-product parity corpus: every value here MUST
# have the same Python and Bash verdict.
RELEASE_TAG_CORPUS: list[tuple[str, bool]] = [
    # Valid (per project release grammar)
    ("v0.0.0", True),
    ("v1.13.0", True),
    ("v0.7.0", True),
    ("v10.20.30", True),
    ("v2.0.0-rc.1", True),  # alphanumeric + numeric prerelease
    ("v1.0.0-alpha", True),  # pure alphanumeric
    ("v1.0.0-alpha.1", True),  # alphanumeric + numeric
    ("v1.0.0-0.3.7", True),  # pure numeric prerelease
    ("v1.0.0-x.7.z.92", True),  # mixed alphanumeric
    ("v1.0.0-rc", True),  # simple alphanumeric
    ("v1.0.0-1", True),  # simple numeric prerelease
    # Invalid
    ("main", False),
    ("", False),
    ("v", False),
    ("v1", False),
    ("v1.2", False),
    ("v1.2.3.4", False),
    ("1.13.0", False),
    # Leading zeros in core
    ("v01.02.003", False),
    ("v01.0.0", False),
    ("v1.02.0", False),
    ("v1.0.03", False),
    # Empty/dot-only prerelease
    ("v1.0.0-", False),  # trailing hyphen
    ("v1.0.0-.", False),  # dot-only prerelease
    ("v1.0.0-..", False),  # empty dot-only
    ("v1.2.3-alpha..1", False),  # empty identifier in prerelease
    # Junk suffix
    ("v1.0.0-!", False),
    # Build metadata (unsupported)
    ("v1.0.0+build", False),
    ("v1.0.0-rc.1+build", False),
    # Mixed alphanumeric with leading digit (not supported
    # in this bounded subset; document as bounded)
    ("v1.0.0-1x", False),
    # Trailing hyphen in alphanumeric identifier
    ("v1.0.0-alpha-", False),
    # Leading zero in numeric prerelease identifier
    ("v1.0.0-01", False),
    ("vgarbage", False),
]


# =============================================================
# Custom exceptions
# =============================================================


class InvariantError(Exception):
    """Base exception for architecture-contract invariant
    violations."""


class NoIfKeyInStepError(InvariantError):
    """Invariant 3: a required supply-chain step has an
    if: condition, is missing, or has a duplicate/decoy."""


class ManualDispatchGuardMissingError(InvariantError):
    """Invariant 5: the Resolve tag step lacks a valid
    release-only manual dispatch guard."""


class MutexChannelMissingError(InvariantError):
    """Invariant 2/6: the docker/metadata-action with.tags
    is missing a distinct semver-v, main-ref, or
    manual-raw rule."""


class PushTriggerSetError(InvariantError):
    """Invariant 1: the push trigger set is not exactly
    {main, v*}."""


class WorkflowRunVGateError(InvariantError):
    """Invariant 4: the workflow_run gate is not
    'conclusion == success AND startsWith(head_branch, v)'."""


class ChannelMetadataError(InvariantError):
    """Invariant 6: the channel-metadata rule / allowed-
    divergence contract is broken (mutually exclusive main
    ref and release semver guards)."""


class TagResolutionRejectError(Exception):
    """The decision model rejects the input as a non-release
    tag."""


# =============================================================
# Helpers
# =============================================================


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_workflow_yaml(path: Path) -> Any:
    """Load a workflow YAML file, normalizing the True-key
    convention that YAML 1.1 applies to 'on:'."""
    config: Any = yaml.safe_load(_read_text(path))
    if isinstance(config, dict) and True in config and "on" not in config:
        config["on"] = config.pop(True)
    return config


def _dump_workflow_yaml(config: Any) -> str:
    """Dump a workflow YAML config, restoring the True-key
    convention."""
    if isinstance(config, dict) and "on" in config and True not in config:
        config[True] = config.pop("on")
    return yaml.dump(config, default_flow_style=False, sort_keys=False)


def _load_publish_steps(path: Path) -> list[dict[str, Any]]:
    """Load the publish-core-image steps from YAML."""
    config = _load_workflow_yaml(path)
    job = config.get("jobs", {}).get("publish-core-image", {})
    if not isinstance(job, dict):
        raise InvariantError(f"No jobs.publish-core-image in {path}")
    return job.get("steps", [])  # type: ignore[no-any-return]


def _load_auto_pin_steps(path: Path) -> list[dict[str, Any]]:
    """Load the auto-digest-bump steps from YAML."""
    config = _load_workflow_yaml(path)
    job = config.get("jobs", {}).get("bump", {})
    if not isinstance(job, dict):
        raise InvariantError(f"No jobs.bump in {path}")
    return job.get("steps", [])  # type: ignore[no-any-return]


def _get_resolve_step_run(path: Path) -> str:
    """Get the `run` script of the Resolve tag + digest step
    in auto-digest-bump.yml via text scanning (preserves bash
    syntax)."""
    text = _read_text(path)
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
# Invariant 1: path-aware push trigger set
# =============================================================


def assert_push_trigger_set(path: Path) -> None:
    """Assert the publish workflow's push trigger set is
    exactly {main, v*}.

    Per Yua 20:57:08 #3: extract a path-aware helper used by
    both the production guard and every corresponding wrong.
    """
    config = _load_workflow_yaml(path)
    on = config.get("on", {})
    if not isinstance(on, dict):
        raise PushTriggerSetError(f"No 'on' block in {path}")
    push = on.get("push", {})
    if not isinstance(push, dict):
        raise PushTriggerSetError(f"No 'push' trigger in {path}")
    branches = push.get("branches", []) or []
    tags = push.get("tags", []) or []
    if "main" not in branches:
        raise PushTriggerSetError(f"push trigger missing 'main' branch: {branches!r}")
    if "v*" not in tags:
        raise PushTriggerSetError(f"push trigger missing 'v*' tag: {tags!r}")
    if sorted(branches) != ["main"]:
        raise PushTriggerSetError(f"push trigger branches not exactly [main]: {branches!r}")
    if sorted(tags) != ["v*"]:
        raise PushTriggerSetError(f"push trigger tags not exactly [v*]: {tags!r}")
    if "workflow_dispatch" not in on:
        raise PushTriggerSetError(
            "workflow_dispatch is a SEPARATE operator trigger and must be present"
        )


# =============================================================
# Invariant 2: structured tag-rule parse (semver/ref/raw)
# =============================================================


def _parse_tag_rules(path: Path) -> list[dict[str, str]]:
    """Parse the docker/metadata-action with.tags into a list
    of distinct rule records.

    Per Yua 20:57:08 #2: parse the static prefix fields and
    the entire enable remainder WITHOUT splitting expression
    commas. The with.tags format is:
        type=KEY[,field=value...] enable=EXPR
    The first comma-separated pair is `type=KEY`. The
    `enable=EXPR` field's value may contain commas inside
    function calls like `startsWith(github.ref, 'refs/tags/v')`.
    We treat `enable=...` as a single field regardless of
    internal commas.

    Per Yua 21:27:31 #1: detect duplicate keys BEFORE dict
    collapse. Reject a repeated key even when both values
    are identical.
    """
    config = _load_workflow_yaml(path)
    job = config.get("jobs", {}).get("publish-core-image", {})
    if not isinstance(job, dict):
        raise InvariantError(f"No jobs.publish-core-image in {path}")
    meta_step = None
    for step in job.get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            meta_step = step
            break
    if meta_step is None:
        raise InvariantError(f"No 'Derive image tags' step in {path}")
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
        # Each rule is a sequence of comma-separated key=value
        # pairs. The `enable=` field may contain commas
        # inside function calls; we treat it as a single
        # field that consumes the rest of the line.
        rule: dict[str, str] = {}
        seen_keys: set[str] = set()
        parts: list[str] = []
        i = 0
        while i < len(stripped):
            if stripped[i : i + 7] == "enable=":
                # The enable field is the rest of the line
                # (preserving internal commas and quoting).
                enable_value = stripped[i + 7 :].strip()
                # Strip surrounding quotes if present
                if (enable_value.startswith('"') and enable_value.endswith('"')) or (
                    enable_value.startswith("'") and enable_value.endswith("'")
                ):
                    enable_value = enable_value[1:-1]
                # Per Yua 21:27:31 #1: detect duplicate keys
                if "enable" in seen_keys:
                    raise MutexChannelMissingError(f"Duplicate key 'enable' in rule line: {line!r}")
                seen_keys.add("enable")
                rule["enable"] = enable_value
                break
            # Find the next comma at the top level
            comma = stripped.find(",", i)
            if comma == -1:
                parts.append(stripped[i:])
                break
            parts.append(stripped[i:comma])
            i = comma + 1
        for part in parts:
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip()
                # Per Yua 21:27:31 #1: detect duplicate keys
                # even when both values are identical.
                if k in seen_keys:
                    raise MutexChannelMissingError(f"Duplicate key {k!r} in rule line: {line!r}")
                seen_keys.add(k)
                rule[k] = v.strip()
        if rule:
            rules.append(rule)
    return rules


# Supported expression grammar (bound and fail closed).
# Per Yua 21:05:45 #2: parse/normalize the supported
# expression grammar and prove each rule is positively
# enabled on its intended channel, disabled on the other
# channels. Reject false/true/token-smear/OR broadening.

# After stripping ${{ ... }} and normalizing whitespace,
# the semver enable MUST match this exact shape.
# Match: startsWith(github.ref, 'refs/tags/v')
SEMVER_PATTERN = (
    r"^startsWith"
    r"\s*"
    r"\("
    r"\s*github\.ref"
    r"\s*,"
    r"\s*"
    r"'refs/tags/v'"
    r"\s*"
    r"\)"
    r"\s*$"
)
SEMVER_ENABLE_SHAPE = re.compile(SEMVER_PATTERN)
# The main-ref enable MUST match this exact shape.
# Match: github.ref == 'refs/heads/main'
MAIN_PATTERN = (
    r"^github\.ref"
    r"\s*=="
    r"\s*"
    r"'refs/heads/main'"
    r"\s*$"
)
MAIN_ENABLE_SHAPE = re.compile(MAIN_PATTERN)
# The manual-raw enable MUST match this exact shape.
# Match: github.event_name == 'workflow_dispatch' &&
# github.event.inputs.tag != ''
RAW_PATTERN = (
    r"^github\.event_name"
    r"\s*=="
    r"\s*"
    r"'workflow_dispatch'"
    r"\s*&&"
    r"\s*"
    r"github\.event\.inputs\.tag"
    r"\s*!="
    r"\s*"
    r"''"
    r"\s*$"
)
RAW_ENABLE_SHAPE = re.compile(RAW_PATTERN)


def _normalize_enable(expr: str) -> str:
    """Strip ${{ ... }} and normalize whitespace.

    Per Yua 21:05:45 #2: bound and fail closed on unsupported
    shapes.
    """
    e = expr.strip()
    # Strip ${{ ... }}
    if e.startswith("${{") and e.endswith("}}"):
        e = e[3:-2].strip()
    # Normalize whitespace
    e = re.sub(r"\s+", " ", e)
    return e


def _assert_supported_shape(expr: str, shape: re.Pattern[str], channel: str) -> None:
    """Assert the expression matches the supported shape for
    the given channel.

    Per Yua 21:05:45 #2: reject false/true/token-smear/
    OR broadening. Do not claim a full GitHub expression
    parser; bound and fail closed on unsupported shapes.
    """
    normalized = _normalize_enable(expr)
    if not shape.match(normalized):
        raise MutexChannelMissingError(
            f"{channel} enable does not match the supported "
            f"shape (expected {shape.pattern!r}, got "
            f"{normalized!r}). Rejecting false/true/token-smear/"
            f"OR broadening."
        )


def assert_distinct_mutex_tags(path: Path) -> None:
    """Assert the docker/metadata-action with.tags has exactly
    three distinct, non-overlapping rules.

    Per Yua 20:57:08 #2: pin complete per-rule truth:
    - semver: type=semver, pattern={...}, prefix=v, enable
      has startsWith(github.ref, 'refs/tags/v')
    - ref: type=ref, event=branch, enable has
      github.ref == 'refs/heads/main'
    - raw: type=raw, value=${{ github.event.inputs.tag }},
      enable has workflow_dispatch AND non-blank input check

    Reject extra overlapping rules.
    """
    rules = _parse_tag_rules(path)
    # Per Yua 21:05:45 #1: pin exact rule cardinality. Reject
    # any extra non-comment metadata rule (e.g., a 4th
    # `type=sha,format=short` rule).
    if len(rules) != 3:
        raise MutexChannelMissingError(
            f"Expected exactly 3 tag rules (semver + ref + raw), got {len(rules)}: {rules!r}"
        )
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
    semver = semver_rules[0]
    ref = ref_rules[0]
    raw = raw_rules[0]
    # Per Yua 21:21:24 #1: pin exact allowed key sets and
    # values for all three metadata rules. Reject unknown
    # or duplicate fields.
    semver_expected_keys = {"type", "pattern", "prefix", "enable"}
    ref_expected_keys = {"type", "event", "enable"}
    raw_expected_keys = {"type", "value", "enable"}
    if set(semver.keys()) != semver_expected_keys:
        raise MutexChannelMissingError(
            f"semver rule keys must be exactly {sorted(semver_expected_keys)}, got {sorted(semver.keys())}: {semver!r}"
        )
    if set(ref.keys()) != ref_expected_keys:
        raise MutexChannelMissingError(
            f"main-ref rule keys must be exactly {sorted(ref_expected_keys)}, got {sorted(ref.keys())}: {ref!r}"
        )
    if set(raw.keys()) != raw_expected_keys:
        raise MutexChannelMissingError(
            f"raw rule keys must be exactly {sorted(raw_expected_keys)}, got {sorted(raw.keys())}: {raw!r}"
        )
    # Per Yua 21:21:24 #1: pin exact values
    if semver.get("pattern") != "{{version}}":
        raise MutexChannelMissingError(
            f"semver rule pattern must be exactly '{{{{version}}}}', got {semver.get('pattern')!r}: {semver!r}"
        )
    if semver.get("prefix") != "v":
        raise MutexChannelMissingError(
            f"semver rule prefix must be exactly 'v', got {semver.get('prefix')!r}: {semver!r}"
        )
    if ref.get("event") != "branch":
        raise MutexChannelMissingError(
            f"main-ref rule event must be exactly 'branch', got {ref.get('event')!r}: {ref!r}"
        )
    if raw.get("value") != "${{ github.event.inputs.tag }}":
        raise MutexChannelMissingError(
            f"raw rule value must be exactly '${{{{ github.event.inputs.tag }}}}', got {raw.get('value')!r}: {raw!r}"
        )
    semver_enable = semver.get("enable", "")
    # Per Yua 21:05:45 #2: strict shape check. Reject
    # false/true/token-smear/OR broadening.
    _assert_supported_shape(semver_enable, SEMVER_ENABLE_SHAPE, "semver")
    # Ref rule enable must gate on main (strict shape)
    ref_enable = ref.get("enable", "")
    _assert_supported_shape(ref_enable, MAIN_ENABLE_SHAPE, "main-ref")
    # Raw rule must gate on workflow_dispatch with non-blank
    # input (strict shape)
    raw_enable = raw.get("enable", "")
    _assert_supported_shape(raw_enable, RAW_ENABLE_SHAPE, "manual-raw")


# =============================================================
# Invariant 3: YAML-parsed per-step check (path-aware)
# =============================================================


# Exact full names of the 5 required supply-chain steps.
# Per Yua 20:57:08 #5: use exact full names, not substrings.
REQUIRED_STEPS_EXACT = {
    "cosign_sign": "Sign the published image (keyless, via GitHub OIDC)",
    "sbom": "Generate SBOM (CycloneDX)",
    "cosign_attest": "Attach SBOM as cosign attestation",
    "trivy_table": "Trivy vulnerability scan (table — always visible in logs)",
    "trivy_sarif": "Trivy vulnerability scan (SARIF — CRITICAL gate)",
}


def _get_step_by_exact_name(name: str, path: Path) -> dict[str, Any] | None:
    """Get a step by exact full name match."""
    for step in _load_publish_steps(path):
        if step.get("name", "") == name:
            return step
    return None


def assert_no_if_key_in_publish_step(name: str, path: Path) -> None:
    """Assert that the named step has no 'if' key.

    Per Yua 20:57:08 #5: use exact full name.
    """
    step = _get_step_by_exact_name(name, path)
    if step is None:
        raise NoIfKeyInStepError(f"Step {name!r} not found in {path}")
    if "if" in step:
        raise NoIfKeyInStepError(f"Step {name!r} has an 'if' key: {step['if']!r}")


def assert_all_required_steps_present(path: Path) -> None:
    """Assert all 5 required steps are present, none have an
    'if' key, and there are no duplicate or decoy names.

    Per Yua 20:57:08 #5: missing, duplicate, renamed-near-
    match, and unrelated substring decoy must fail for their
    intended reason.
    """
    steps = _load_publish_steps(path)
    # Check exact presence: each required step must be present
    # by exact name
    for key, exact_name in REQUIRED_STEPS_EXACT.items():
        step = _get_step_by_exact_name(exact_name, path)
        if step is None:
            raise NoIfKeyInStepError(f"Required step {exact_name!r} is missing")
        if "if" in step:
            raise NoIfKeyInStepError(
                f"Required step {exact_name!r} has an 'if' key: {step['if']!r}"
            )
    # Check no duplicate: each exact name appears at most once
    name_counts: dict[str, int] = {}
    for step in steps:
        n = step.get("name", "")
        if n in REQUIRED_STEPS_EXACT.values():
            name_counts[n] = name_counts.get(n, 0) + 1
    for n, count in name_counts.items():
        if count > 1:
            raise NoIfKeyInStepError(f"Duplicate step name: {n!r} appears {count} times")
    # Check no renamed-near-match decoy: a step that has a
    # name that's a near-match of a required step (e.g.,
    # "Sign the published image (keyless)" without the full
    # "via GitHub OIDC" suffix)
    for step in steps:
        n = step.get("name", "")
        if n in REQUIRED_STEPS_EXACT.values():
            continue
        # Check if the name contains a required step's name
        # as a substring (i.e., it's a renamed-near-match
        # decoy). We require EXACT match, so this is a
        # decoy and should fail.
        for exact_name in REQUIRED_STEPS_EXACT.values():
            if n != exact_name and (exact_name in n or n in exact_name):
                raise NoIfKeyInStepError(
                    f"Decoy step name {n!r} is a near-match of required step {exact_name!r}"
                )


# =============================================================
# Invariant 4: path-aware workflow_run v* gate
# =============================================================


# Per Yua 21:05:45 #3: prove the supported job-if shape
# semantically. The supported shape is:
#   github.event_name == 'workflow_dispatch' ||
#   (github.event.workflow_run.conclusion == 'success'
#    && startsWith(github.event.workflow_run.head_branch, 'v'))
# After stripping ${{ ... }} and normalizing whitespace.
JOB_IF_PATTERN = (
    r"^github\.event_name"
    r"\s*=="
    r"\s*"
    r"'workflow_dispatch'"
    r"\s*\|\|"
    r"\s*\("
    r"\s*github\.event\.workflow_run\.conclusion"
    r"\s*=="
    r"\s*"
    r"'success'"
    r"\s*&&"
    r"\s*startsWith"
    r"\s*\("
    r"\s*github\.event\.workflow_run\.head_branch"
    r"\s*,"
    r"\s*"
    r"'v'"
    r"\s*\)"
    r"\s*\)"
    r"\s*$"
)
JOB_IF_SHAPE = re.compile(JOB_IF_PATTERN)


def assert_workflow_run_v_gate(path: Path) -> None:
    """Assert the auto-pin workflow gates on workflow_run with
    conclusion == 'success' AND startsWith(head_branch, 'v').

    Per Yua 20:57:08 #3: extract a path-aware helper used by
    both the production guard and every corresponding wrong.
    Per Yua 20:57:08 #4: the gate is at jobs.bump.if, NOT at
    workflow_run.branches.
    Per Yua 21:05:45 #3: prove the supported job-if shape
    semantically. Reject false/true/token-smear/OR broadening.
    """
    config = _load_workflow_yaml(path)
    job = config.get("jobs", {}).get("bump", {})
    if not isinstance(job, dict):
        raise WorkflowRunVGateError(f"No jobs.bump in {path}")
    if_conditions = job.get("if", "")
    if not isinstance(if_conditions, str):
        raise WorkflowRunVGateError(f"jobs.bump.if is not a string: {if_conditions!r}")
    # Per Yua 21:05:45 #3: strict shape check. Reject
    # false/true/token-smear/OR broadening.
    normalized = _normalize_enable(if_conditions)
    if not JOB_IF_SHAPE.match(normalized):
        raise WorkflowRunVGateError(
            f"jobs.bump.if does not match the supported shape. "
            f"Expected: github.event_name == 'workflow_dispatch' || "
            f"(github.event.workflow_run.conclusion == 'success' && "
            f"startsWith(github.event.workflow_run.head_branch, 'v')). "
            f"Got: {normalized!r}"
        )


# =============================================================
# Invariant 5: bash guard placement and control flow
# =============================================================


def _run_bash_harness(guard_block: str, tag_value: str) -> int:
    """Execute a guard block with TAG=<tag_value> and return
    the exit code.

    Per Yua 20:57:08 #1: the bash proof must actually run.
    The guard_block is the bash code to test (e.g., the
    if ! [[ \"$TAG\" == v* ]]; then ... exit 1; fi block).
    """
    script = f"#!/bin/bash\nset -euo pipefail\nTAG={tag_value!r}\n{guard_block}\necho SUCCESS\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
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
        return result.returncode
    finally:
        Path(path).unlink(missing_ok=True)


def _extract_guard_block(run_script: str) -> str | None:
    """Extract the if ! [[ \"$TAG\" =~ semver ]]; then ... exit N;
    fi block from the Resolve step's run script.

    Per Yua 20:57:08 #1: require the nonzero exit INSIDE the
    matched if block before its fi, not merely any later exit.
    Per Yua 21:21:24 #4 + 21:27:31 #2: the guard uses the
    bounded SemVer 2.0.0 grammar (Bash POSIX ERE: capturing
    groups) to align with the decision model.
    """
    # Find the if block: if ! [[ "$TAG" =~ semver ]]; then
    # ... exit N; fi
    pattern = (
        r"if\s+!\s*\[\[\s*\"?\$TAG\"?\s*=~\s*\^v"
        r"[^;]*;"
        r"(.*?)"
        r"^\s*fi\b"
    )
    m = re.search(pattern, run_script, re.DOTALL | re.MULTILINE)
    if not m:
        return None
    return m.group(0)


def assert_release_only_manual_dispatch_guard(path: Path) -> None:
    """Assert the Resolve tag + digest step has a valid
    release-only manual dispatch guard.

    Per Yua 20:57:08 #1: extract the exact guard block and
    execute it with TAG values. Prove valid release tags
    return zero and main, empty, and malformed v-prefixed
    tags return nonzero. Require the nonzero exit INSIDE
    the matched if block before its fi.

    Per Yua 20:57:08 #1 (additional): the guard must include
    the tag emission/use anchor (it must appear before
    tag is emitted or used in /v2/${IMAGE}/manifests/${TAG}).
    """
    run_script = _get_resolve_step_run(path)
    if not run_script:
        raise ManualDispatchGuardMissingError(
            "auto-digest-bump.yml has no Resolve tag + digest step with a run script"
        )
    # Extract the guard block
    guard_block = _extract_guard_block(run_script)
    if guard_block is None:
        raise ManualDispatchGuardMissingError(
            "Resolve tag + digest step lacks the guard block: "
            'if ! [[ "$TAG" == v* ]]; then ... exit N; fi'
        )
    # Require the guard appears BEFORE the tag output emission
    # or the manifest request
    guard_pos = run_script.find(guard_block)
    output_match = re.search(
        r'>>\s*"?\$GITHUB_OUTPUT"?',
        run_script,
    )
    if output_match:
        output_pos = output_match.start()
        if guard_pos > output_pos:
            raise ManualDispatchGuardMissingError(
                "Resolve tag + digest step has the guard AFTER "
                "the tag is emitted. The guard must appear "
                "before tag is emitted or used."
            )
    # The guard must be executable (not inside a comment).
    # Check that the if ! line is not preceded by # on the
    # same line.
    for line in guard_block.split("\n"):
        if "if ! [[" in line and "$TAG" in line:
            stripped = line.strip()
            if stripped.startswith("#"):
                raise ManualDispatchGuardMissingError(
                    "Resolve tag + digest step guard is inside "
                    "a comment. The guard must be executable."
                )
    # Executable bash proof: actually run the guard with
    # various TAG values and check the exit code.
    # Per Yua 21:05:45 #4: the guard uses the same bounded
    # release grammar as the resolver. Valid: semver tags.
    # Invalid: anything else (including v* glob matches like
    # vgarbage).
    valid_tags = [
        "v1.13.0",
        "v0.7.0",
        "v2.0.0-rc.1",
        "v1.0.0-alpha",
        "v1.0.0-alpha.1",
        "v1.0.0-0.3.7",
        "v1.0.0-x.7.z.92",
        "v10.20.30",
        "v0.0.0",
    ]
    invalid_tags = [
        "main",
        "",
        "refs/tags/v1.0.0",
        "1.13.0",
        "vgarbage",
        "v",
        "v1",
        "v1.2",
        "v1.2.3.4",
        "v01.02.003",
        "v01.0.0",
        "v1.02.0",
        "v1.0.03",
        "v1.0.0-",
        "v1.0.0-.",
        "v1.0.0-..",
        "v1.0.0-!",
        "v1.0.0+build",
        "v1.0.0-rc.1+build",
        "v1.2.3-alpha..1",
    ]
    for tag in valid_tags:
        rc = _run_bash_harness(guard_block, tag)
        if rc != 0:
            raise ManualDispatchGuardMissingError(
                f"Guard rejected valid release tag {tag!r} with exit code {rc}"
            )
    for tag in invalid_tags:
        rc = _run_bash_harness(guard_block, tag)
        if rc == 0:
            raise ManualDispatchGuardMissingError(
                f"Guard accepted invalid tag {tag!r} with exit code 0 (should be nonzero)"
            )


# =============================================================
# Invariant 6: channel-metadata / allowed-divergence
# =============================================================


def assert_release_channel_consumption(path: Path) -> None:
    """Assert the channel-metadata rule / allowed-divergence
    contract.

    Per Yua 20:57:08 #4: Inv6 is the accepted mutually
    exclusive channel-metadata rule / allowed-divergence
    contract. The publish workflow's with.tags must have
    mutually exclusive main ref and release semver guards,
    AND the contract is that divergence is ALLOWED (not
    GUARANTEED). The same assert_distinct_mutex_tags helper
    proves the mutex; the test-local model proves the
    allowed-divergence contract.

    Per Yua 20:57:08 #3: extract a path-aware helper used by
    both the production guard and every corresponding wrong.
    """
    assert_distinct_mutex_tags(path)
    # The contract is that divergence is ALLOWED. The
    # resolved tags from the semver and ref rules are
    # guaranteed to be distinct because their enable
    # conditions are mutually exclusive. We prove this by
    # checking the enable conditions don't overlap.
    rules = _parse_tag_rules(path)
    semver_rules = [r for r in rules if r.get("type") == "semver"]
    ref_rules = [r for r in rules if r.get("type") == "ref" and r.get("event") == "branch"]
    semver_enable = semver_rules[0].get("enable", "")
    ref_enable = ref_rules[0].get("enable", "")
    # The semver enable must contain refs/tags/v and NOT
    # refs/heads/main
    if "refs/heads/main" in semver_enable:
        raise ChannelMetadataError(f"semver enable overlaps with main: {semver_enable!r}")
    # The ref enable must contain refs/heads/main and NOT
    # refs/tags/v
    if "refs/tags/v" in ref_enable:
        raise ChannelMetadataError(f"main-ref enable overlaps with tag: {ref_enable!r}")


# =============================================================
# Decision model: tag resolution contract
# =============================================================


def _resolve_release_tag(explicit: str | None, latest: str | None) -> str:
    """Test-local model of the desired decision contract.

    Per Yua 20:57:08 #6: use the release tag grammar
    RELEASE_TAG_GRAMMAR. Add explicit invalid-v-prefix cases
    for both explicit and latest fallback.

    Contract:
      - explicit v* (matches RELEASE_TAG_GRAMMAR) -> same v
      - blank + valid latest v* (matches RELEASE_TAG_GRAMMAR)
        -> that latest v
      - explicit main -> reject
      - blank + main/empty/malformed latest -> reject
    """
    if explicit is not None and explicit != "":
        if explicit == "main":
            raise TagResolutionRejectError("Explicit 'main' is not a release tag")
        if not RELEASE_TAG_GRAMMAR.match(explicit):
            raise TagResolutionRejectError(
                f"Explicit input {explicit!r} is not a valid "
                f"v-prefixed release tag (must match "
                f"{RELEASE_TAG_GRAMMAR.pattern})"
            )
        return explicit
    # Blank input: fall back to latest
    if latest is None or latest == "":
        raise TagResolutionRejectError("Blank input with no latest value")
    if latest == "main":
        raise TagResolutionRejectError("Latest is 'main', not a release tag")
    if not RELEASE_TAG_GRAMMAR.match(latest):
        raise TagResolutionRejectError(
            f"Latest {latest!r} is not a valid v-prefixed "
            f"release tag (must match {RELEASE_TAG_GRAMMAR.pattern})"
        )
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
    assert_push_trigger_set(PUBLISH_WF)


def test_invariant_2_mutex_release_channels() -> None:
    """Invariant 2: distinct mutually exclusive semver-v,
    main-ref, and manual-raw rules in
    docker/metadata-action with.tags."""
    assert_distinct_mutex_tags(PUBLISH_WF)


def test_invariant_3_per_supply_chain_step_independent() -> None:
    """Invariant 3: all 5 required supply-chain steps are
    present, none have an 'if' key, and there are no
    duplicate or decoy names."""
    assert_all_required_steps_present(PUBLISH_WF)
    for key, exact_name in REQUIRED_STEPS_EXACT.items():
        assert_no_if_key_in_publish_step(exact_name, PUBLISH_WF)


def test_invariant_4_workflow_run_v_gate() -> None:
    """Invariant 4: auto-pin gates on workflow_run with
    conclusion == 'success' AND startsWith(head_branch, 'v').

    Per Yua 20:57:08 #4: the gate is at jobs.bump.if, NOT
    at workflow_run.branches.
    """
    assert_workflow_run_v_gate(AUTO_PIN_WF)


@pytest.mark.xfail(
    strict=True,
    raises=ManualDispatchGuardMissingError,
    reason=(
        "Invariant 5 FAIL (per Yua 20:57:08 #1): the current "
        "auto-digest-bump.yml Resolve tag + digest step lacks a "
        "valid bash guard with proper placement and control "
        "flow. After the workflow fix, this test flips green."
    ),
)
def test_invariant_5_release_only_manual_dispatch_guard() -> None:
    """Invariant 5: the Resolve tag + digest step has a valid
    release-only manual dispatch guard."""
    assert_release_only_manual_dispatch_guard(AUTO_PIN_WF)


def test_invariant_6_channel_metadata_allowed_divergence() -> None:
    """Invariant 6: channel-metadata rule / allowed-divergence
    contract.

    Per Yua 20:57:08 #4: the publish workflow's with.tags
    has mutually exclusive main ref and release semver
    guards. Divergence between main and v* digests is
    ALLOWED, not GUARANTEED.
    """
    assert_release_channel_consumption(PUBLISH_WF)


# =============================================================
# 1 Strict Red (reproduces the hardening defect)
# =============================================================


@pytest.mark.xfail(
    strict=True,
    raises=ManualDispatchGuardMissingError,
    reason=(
        "Hardening defect (per Yua 19:54:29 + 20:57:08 #1): "
        "auto-digest-bump.yml does NOT enforce release-only "
        "manual dispatch. After the workflow fix, this test "
        "flips green."
    ),
)
def test_red_hardening_defect_manual_dispatch_main() -> None:
    """Strict red: the current auto-digest-bump workflow
    accepts a non-release tag from manual dispatch."""
    assert_release_only_manual_dispatch_guard(AUTO_PIN_WF)


# =============================================================
# Wrong-Fixture Mutation Tests
# =============================================================


@pytest.fixture
def fixture_dir() -> Any:
    """Create a temporary directory for mutated workflow copies."""
    d = tempfile.mkdtemp(prefix="issue449-fixture-")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


# --- Invariant 1 wrong-fixtures (shared helper) ---


def _mutate_yaml_remove_v_tag_trigger(src: Path, dst: Path) -> None:
    """Remove the v* tag trigger from the publish workflow."""
    config = _load_workflow_yaml(src)
    on = config.get("on", {})
    if isinstance(on, dict):
        push = on.get("push", {})
        if isinstance(push, dict):
            push["tags"] = []
    dst.write_text(_dump_workflow_yaml(config))


def test_wrong_fixture_inv1_remove_v_tag_trigger(fixture_dir: Any) -> None:
    """Wrong-fixture: removing the v* tag trigger breaks
    Invariant 1. Invokes the same production helper."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_remove_v_tag_trigger(PUBLISH_WF, dst)
    try:
        assert_push_trigger_set(dst)
    except PushTriggerSetError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: removing the v* tag trigger did "
        "NOT cause the production helper to raise."
    )


# --- Invariant 2 wrong-fixtures (shared helper) ---


def _mutate_yaml_remove_rule_by_type(src: Path, dst: Path, rule_type: str) -> None:
    """Remove a tag rule by type (semver, ref, raw)."""
    config = _load_workflow_yaml(src)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                lines = tags_value.split("\n")
                new_lines = []
                in_target_rule = False
                for line in lines:
                    stripped = line.strip()
                    if f"type={rule_type}" in stripped:
                        in_target_rule = True
                        continue
                    if in_target_rule and stripped.startswith("#"):
                        in_target_rule = False
                        continue
                    in_target_rule = False
                    new_lines.append(line)
                with_block["tags"] = "\n".join(new_lines)
            break
    dst.write_text(_dump_workflow_yaml(config))


def test_wrong_fixture_inv2_missing_main_ref_rule(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: missing main-ref rule breaks Invariant 2."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_remove_rule_by_type(PUBLISH_WF, dst, "ref")
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: missing main-ref rule did NOT cause the production helper to raise."
    )


def test_wrong_fixture_inv2_missing_semver_rule(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: missing semver rule breaks Invariant 2."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_remove_rule_by_type(PUBLISH_WF, dst, "semver")
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: missing semver rule did NOT cause the production helper to raise."
    )


def test_wrong_fixture_inv2_missing_raw_rule(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: missing manual-raw rule breaks Invariant 2."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_remove_rule_by_type(PUBLISH_WF, dst, "raw")
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: missing manual-raw rule did NOT cause the production helper to raise."
    )


def test_wrong_fixture_inv2_semver_enabled_on_main(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: semver rule enabled on main breaks
    Invariant 2."""
    dst = fixture_dir / "publish-core-image.yml"
    config = _load_workflow_yaml(PUBLISH_WF)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                # Change the semver enable to gate on main
                tags_value = tags_value.replace(
                    "startsWith(github.ref, 'refs/tags/v')",
                    "github.ref == 'refs/heads/main'",
                )
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: semver enabled on main did NOT cause the production helper to raise."
    )


def test_wrong_fixture_inv2_main_enabled_on_tag(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: main-ref rule enabled on tag breaks
    Invariant 2."""
    dst = fixture_dir / "publish-core-image.yml"
    config = _load_workflow_yaml(PUBLISH_WF)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                tags_value = tags_value.replace(
                    "github.ref == 'refs/heads/main'",
                    "startsWith(github.ref, 'refs/tags/v')",
                )
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: main enabled on tag did NOT cause the production helper to raise."
    )


def test_wrong_fixture_inv2_raw_enabled_outside_dispatch(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: raw rule enabled outside workflow_dispatch
    breaks Invariant 2."""
    dst = fixture_dir / "publish-core-image.yml"
    config = _load_workflow_yaml(PUBLISH_WF)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                # Change the raw enable to not gate on dispatch
                tags_value = tags_value.replace(
                    "workflow_dispatch",
                    "push",
                )
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: raw enabled outside dispatch did "
        "NOT cause the production helper to raise."
    )


def test_wrong_fixture_inv2_raw_allows_blank(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: raw rule allows blank input breaks
    Invariant 2."""
    dst = fixture_dir / "publish-core-image.yml"
    config = _load_workflow_yaml(PUBLISH_WF)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                # Remove the non-blank check
                tags_value = tags_value.replace(
                    " && github.event.inputs.tag != ''",
                    "",
                )
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: raw allowing blank did NOT cause the production helper to raise."
    )


def test_wrong_fixture_inv2_token_smear(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: token-smear across records breaks
    Invariant 2.

    A token-smear is when the semver rule's enable contains
    refs/heads/main (the main ref's token), causing the
    semver and main rules to have overlapping enables.
    """
    dst = fixture_dir / "publish-core-image.yml"
    config = _load_workflow_yaml(PUBLISH_WF)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                # Replace the semver enable's startsWith with
                # the main enable's github.ref check. This
                # smears the main token into the semver rule.
                tags_value = tags_value.replace(
                    "startsWith(github.ref, 'refs/tags/v')",
                    "github.ref == 'refs/heads/main'",
                )
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: token-smear across records did "
        "NOT cause the production helper to raise."
    )


def test_wrong_fixture_inv2_missing_prefix(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: missing prefix=v on semver breaks
    Invariant 2."""
    dst = fixture_dir / "publish-core-image.yml"
    config = _load_workflow_yaml(PUBLISH_WF)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                tags_value = tags_value.replace(",prefix=v", "")
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: missing prefix=v did NOT cause the production helper to raise."
    )


def test_wrong_fixture_inv2_missing_value(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: missing value= on raw breaks
    Invariant 2."""
    dst = fixture_dir / "publish-core-image.yml"
    config = _load_workflow_yaml(PUBLISH_WF)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                tags_value = tags_value.replace(
                    "value=${{ github.event.inputs.tag }}",
                    "",
                )
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: missing value= did NOT cause the production helper to raise."
    )


# --- Invariant 3 wrong-fixtures (path-aware checker) ---


def _mutate_yaml_semver_disabled(src: Path, dst: Path) -> None:
    """Prepend `false &&` to the semver enable (rule disabled)."""
    config = _load_workflow_yaml(src)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                tags_value = tags_value.replace(
                    "enable=${{ startsWith(github.ref, 'refs/tags/v') }}",
                    "enable=${{ false && startsWith(github.ref, 'refs/tags/v') }}",
                )
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))


def test_wrong_fixture_inv2_semver_disabled_by_false(fixture_dir: Any) -> None:
    """Wrong-fixture: semver disabled by `false &&` breaks
    Invariant 2.

    Per Yua 21:05:45 #2: token-presence check accepts this
    because startsWith and refs/tags/v are still present.
    Strict shape check rejects.
    """
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_semver_disabled(PUBLISH_WF, dst)
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: semver disabled by `false &&` did NOT cause the production helper to raise."
    )


def _mutate_yaml_main_disabled(src: Path, dst: Path) -> None:
    """Prepend `false &&` to the main enable (rule disabled)."""
    config = _load_workflow_yaml(src)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                tags_value = tags_value.replace(
                    "enable=${{ github.ref == 'refs/heads/main' }}",
                    "enable=${{ false && github.ref == 'refs/heads/main' }}",
                )
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))


def test_wrong_fixture_inv2_main_disabled_by_false(fixture_dir: Any) -> None:
    """Wrong-fixture: main disabled by `false &&` breaks Invariant 2."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_main_disabled(PUBLISH_WF, dst)
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: main disabled by `false &&` did NOT cause the production helper to raise."
    )


def _mutate_yaml_raw_disabled(src: Path, dst: Path) -> None:
    """Prepend `false &&` to the raw enable (rule disabled)."""
    config = _load_workflow_yaml(src)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                tags_value = tags_value.replace(
                    "enable=${{ github.event_name == 'workflow_dispatch'",
                    "enable=${{ false && github.event_name == 'workflow_dispatch'",
                )
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))


def test_wrong_fixture_inv2_raw_disabled_by_false(fixture_dir: Any) -> None:
    """Wrong-fixture: raw disabled by `false &&` breaks Invariant 2."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_raw_disabled(PUBLISH_WF, dst)
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: raw disabled by `false &&` did NOT cause the production helper to raise."
    )


def _mutate_yaml_extra_sha_rule(src: Path, dst: Path) -> None:
    """Add an extra `type=sha,format=short` rule (4th rule)."""
    config = _load_workflow_yaml(src)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                tags_value = tags_value + "\ntype=sha,format=short"
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))


def test_wrong_fixture_inv2_extra_sha_rule(fixture_dir: Any) -> None:
    """Wrong-fixture: extra `type=sha,format=short` rule breaks
    Invariant 2.

    Per Yua 21:05:45 #1: cardinality check rejects any extra
    non-comment metadata rule.
    """
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_extra_sha_rule(PUBLISH_WF, dst)
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: extra sha rule did NOT cause the production helper to raise."
    )


def _mutate_yaml_job_if_false(src: Path, dst: Path) -> None:
    """Prepend `false &&` to the job-if (gate disabled)."""
    config = _load_workflow_yaml(src)
    job = config.get("jobs", {}).get("bump", {})
    if isinstance(job, dict):
        if_conditions = job.get("if", "")
        if isinstance(if_conditions, str):
            job["if"] = "false && " + if_conditions
    dst.write_text(_dump_workflow_yaml(config))


def test_wrong_fixture_inv4_job_if_disabled_by_false(fixture_dir: Any) -> None:
    """Wrong-fixture: job-if disabled by `false &&` breaks
    Invariant 4.

    Per Yua 21:05:45 #3: token-presence check accepts this.
    Strict shape check rejects.
    """
    dst = fixture_dir / "auto-digest-bump.yml"
    _mutate_yaml_job_if_false(AUTO_PIN_WF, dst)
    try:
        assert_workflow_run_v_gate(dst)
    except WorkflowRunVGateError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: job-if disabled by `false &&` did NOT cause the production helper to raise."
    )


def _mutate_yaml_raw_value_main(src: Path, dst: Path) -> None:
    """Change the raw rule's value to `main`."""
    config = _load_workflow_yaml(src)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                tags_value = tags_value.replace(
                    "value=${{ github.event.inputs.tag }}",
                    "value=main",
                )
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))


def test_wrong_fixture_inv2_raw_value_main(fixture_dir: Any) -> None:
    """Wrong-fixture: raw value=main recreates channel overlap.

    Per Yua 21:21:24 #1: pin raw value to exactly
    `${{ github.event.inputs.tag }}`. Hard-coding `main`
    lets a manual rule recreate the channel overlap.
    """
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_raw_value_main(PUBLISH_WF, dst)
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: raw value=main did NOT cause the production helper to raise."
    )


def _mutate_yaml_semver_pattern_main(src: Path, dst: Path) -> None:
    """Change the semver rule's pattern from `{{version}}` to `main`."""
    config = _load_workflow_yaml(src)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                tags_value = tags_value.replace(
                    "pattern={{version}}",
                    "pattern=main",
                )
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))


def test_wrong_fixture_inv2_semver_pattern_main(fixture_dir: Any) -> None:
    """Wrong-fixture: semver pattern=main breaks Invariant 2.

    Per Yua 21:21:24 #1: pin semver pattern to exactly
    `{{version}}`.
    """
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_semver_pattern_main(PUBLISH_WF, dst)
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: semver pattern=main did NOT cause the production helper to raise."
    )


def _mutate_yaml_unknown_field(src: Path, dst: Path) -> None:
    """Add an unknown `priority=999` field to the semver rule."""
    config = _load_workflow_yaml(src)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                # Add a priority=999 field to the semver rule
                tags_value = tags_value.replace(
                    "type=semver,pattern={{version}}",
                    "type=semver,pattern={{version}},priority=999",
                )
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))


def test_wrong_fixture_inv2_unknown_field(fixture_dir: Any) -> None:
    """Wrong-fixture: unknown `priority=999` field breaks
    Invariant 2.

    Per Yua 21:21:24 #1: reject unknown fields.
    """
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_unknown_field(PUBLISH_WF, dst)
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: unknown field did NOT cause the production helper to raise."
    )


def test_control_malformed_prerelease_rejected() -> None:
    """Control 8: malformed prerelease values are rejected
    by the bounded SemVer grammar.

    Per Yua 21:21:24 #2 + #4: the resolver rejects malformed
    prerelease (empty, dot-only, trailing hyphen/dot).
    """
    for bad in [
        "v1.0.0-",  # trailing hyphen
        "v1.0.0-.",  # dot-only prerelease
        "v1.0.0-..",  # empty dot-only
        "v1.0.0-!",  # junk suffix
        "v1.0.0+build",  # build metadata not supported
        "v1.0.0-rc.1+build",  # build metadata not supported
    ]:
        with pytest.raises(TagResolutionRejectError):
            _resolve_release_tag(bad, None)
        with pytest.raises(TagResolutionRejectError):
            _resolve_release_tag("", bad)


def test_control_leading_zero_core_rejected() -> None:
    """Control 9: leading-zero core identifiers are rejected.

    Per Yua 21:21:24 #2 + #4: SemVer 2.0.0 requires non-zero-
    prefixed core numbers.
    """
    for bad in [
        "v01.02.003",
        "v01.0.0",
        "v1.02.0",
        "v1.0.03",
    ]:
        with pytest.raises(TagResolutionRejectError):
            _resolve_release_tag(bad, None)
        with pytest.raises(TagResolutionRejectError):
            _resolve_release_tag("", bad)


def test_control_python_bash_grammar_parity() -> None:
    """Control 10: Python, Bash, and CORRECTED_GUARD grammars
    agree on every value in the cross-product corpus.

    Per Yua 21:27:31 #2 + 21:36:58 #1: the Python resolver,
    the executable Bash proof, AND the actual CORRECTED_GUARD
    regex must share one bounded project release grammar
    source. The cross-product parity test verifies that for
    every value in RELEASE_TAG_CORPUS, all three return the
    same verdict.
    """
    # Extract the regex from CORRECTED_GUARD (the actual
    # synthetic guard that represents the workflow fix).
    guard_regex = _extract_guard_regex(CORRECTED_GUARD)
    for value, expected in RELEASE_TAG_CORPUS:
        python_match = bool(RELEASE_TAG_GRAMMAR_PYTHON.match(value))
        bash_match = _run_bash_regex_test(RELEASE_TAG_GRAMMAR_BASH, value)
        guard_match = _run_bash_regex_test(guard_regex, value)
        if python_match != expected or bash_match != expected or guard_match != expected:
            raise AssertionError(
                f"Grammar parity failed for {value!r}: "
                f"expected {expected}, python={python_match}, "
                f"bash={bash_match}, guard={guard_match}"
            )
        if python_match != bash_match or python_match != guard_match:
            raise AssertionError(
                f"Verdicts disagree for {value!r}: "
                f"python={python_match}, bash={bash_match}, "
                f"guard={guard_match}"
            )


def _mutate_yaml_add_if_to_step(src: Path, dst: Path, exact_name: str, if_condition: str) -> None:
    """Add an 'if' key to the named step."""
    config = _load_workflow_yaml(src)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if step.get("name", "") == exact_name:
            step["if"] = if_condition
            break
    dst.write_text(_dump_workflow_yaml(config))


def _mutate_yaml_remove_step(src: Path, dst: Path, exact_name: str) -> None:
    """Remove the named step."""
    config = _load_workflow_yaml(src)
    steps = config.get("jobs", {}).get("publish-core-image", {}).get("steps", [])
    config["jobs"]["publish-core-image"]["steps"] = [
        s for s in steps if s.get("name", "") != exact_name
    ]
    dst.write_text(_dump_workflow_yaml(config))


def _mutate_yaml_duplicate_step(src: Path, dst: Path, exact_name: str) -> None:
    """Duplicate the named step."""
    config = _load_workflow_yaml(src)
    steps = config.get("jobs", {}).get("publish-core-image", {}).get("steps", [])
    for s in steps:
        if s.get("name", "") == exact_name:
            idx = steps.index(s)
            dup = copy.deepcopy(s)
            dup["name"] = exact_name + " (copy)"
            steps.insert(idx + 1, dup)
            break
    dst.write_text(_dump_workflow_yaml(config))


def _mutate_yaml_renamed_near_match_decoy(src: Path, dst: Path, exact_name: str) -> None:
    """Add a renamed-near-match decoy step.

    The decoy has a name that contains the required step's
    name as a substring but is NOT an exact match.
    """
    config = _load_workflow_yaml(src)
    decoy_step = {
        "name": exact_name + " (decoy)",
        "run": "echo 'this is a decoy'",
    }
    steps = config.get("jobs", {}).get("publish-core-image", {}).get("steps", [])
    steps.append(decoy_step)
    dst.write_text(_dump_workflow_yaml(config))


def _mutate_yaml_unrelated_substring_decoy(src: Path, dst: Path, exact_name: str) -> None:
    """Add an unrelated substring decoy step.

    The decoy has a name that contains the required step's
    FULL name as a substring, but is NOT an exact match.
    For example: required name = "Sign the published image
    (keyless, via GitHub OIDC)"; decoy = "Sign the published
    image (keyless, via GitHub OIDC) (decoy)".
    """
    config = _load_workflow_yaml(src)
    # The decoy is the exact name + " (decoy)" suffix
    decoy_step = {
        "name": exact_name + " (decoy)",
        "run": "echo 'this is an unrelated decoy'",
    }
    steps = config.get("jobs", {}).get("publish-core-image", {}).get("steps", [])
    steps.append(decoy_step)
    dst.write_text(_dump_workflow_yaml(config))


@pytest.mark.parametrize(
    "step_key,exact_name,if_condition",
    [
        (
            "cosign_sign_uses_event_name",
            "Sign the published image (keyless, via GitHub OIDC)",
            "${{ github.event_name == 'workflow_dispatch' }}",
        ),
        (
            "sbom_uses_startsWith",
            "Generate SBOM (CycloneDX)",
            "${{ startsWith(github.ref, 'refs/heads/main') }}",
        ),
        (
            "cosign_attest_uses_success",
            "Attach SBOM as cosign attestation",
            "${{ success() }}",
        ),
        (
            "trivy_table_uses_ref_type",
            "Trivy vulnerability scan (table — always visible in logs)",
            "${{ github.ref_type == 'branch' }}",
        ),
        (
            "trivy_sarif_uses_event_name",
            "Trivy vulnerability scan (SARIF — CRITICAL gate)",
            "${{ github.event_name == 'push' }}",
        ),
    ],
)
def test_wrong_fixture_inv3_add_if_key_to_step(
    step_key: str,
    exact_name: str,
    if_condition: str,
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: adding a realistic trigger expression
    breaks Invariant 3."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_add_if_to_step(PUBLISH_WF, dst, exact_name, if_condition)
    try:
        assert_no_if_key_in_publish_step(exact_name, dst)
    except NoIfKeyInStepError:
        return
    raise InvariantError(
        f"Wrong-fixture FAIL: adding 'if' ({if_condition!r}) to "
        f"step {step_key!r} did NOT cause the production "
        f"helper to raise."
    )


@pytest.mark.parametrize(
    "exact_name",
    list(REQUIRED_STEPS_EXACT.values()),
)
def test_wrong_fixture_inv3_missing_step(exact_name: str, fixture_dir: Any) -> None:
    """Wrong-fixture: removing a required step breaks Invariant 3."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_remove_step(PUBLISH_WF, dst, exact_name)
    try:
        assert_all_required_steps_present(dst)
    except NoIfKeyInStepError:
        return
    raise InvariantError(
        f"Wrong-fixture FAIL: removing step {exact_name!r} "
        f"did NOT cause the production helper to raise."
    )


@pytest.mark.parametrize(
    "exact_name",
    list(REQUIRED_STEPS_EXACT.values()),
)
def test_wrong_fixture_inv3_duplicate_step(exact_name: str, fixture_dir: Any) -> None:
    """Wrong-fixture: duplicating a required step breaks
    Invariant 3."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_duplicate_step(PUBLISH_WF, dst, exact_name)
    try:
        assert_all_required_steps_present(dst)
    except NoIfKeyInStepError:
        return
    raise InvariantError(
        f"Wrong-fixture FAIL: duplicating step {exact_name!r} "
        f"did NOT cause the production helper to raise."
    )


@pytest.mark.parametrize(
    "exact_name",
    list(REQUIRED_STEPS_EXACT.values()),
)
def test_wrong_fixture_inv3_renamed_near_match_decoy(exact_name: str, fixture_dir: Any) -> None:
    """Wrong-fixture: adding a renamed-near-match decoy breaks
    Invariant 3."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_renamed_near_match_decoy(PUBLISH_WF, dst, exact_name)
    try:
        assert_all_required_steps_present(dst)
    except NoIfKeyInStepError:
        return
    raise InvariantError(
        f"Wrong-fixture FAIL: renamed-near-match decoy for "
        f"step {exact_name!r} did NOT cause the production "
        f"helper to raise."
    )


@pytest.mark.parametrize(
    "exact_name",
    list(REQUIRED_STEPS_EXACT.values()),
)
def test_wrong_fixture_inv3_unrelated_substring_decoy(exact_name: str, fixture_dir: Any) -> None:
    """Wrong-fixture: adding an unrelated substring decoy breaks
    Invariant 3."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_unrelated_substring_decoy(PUBLISH_WF, dst, exact_name)
    try:
        assert_all_required_steps_present(dst)
    except NoIfKeyInStepError:
        return
    raise InvariantError(
        f"Wrong-fixture FAIL: unrelated substring decoy for "
        f"step {exact_name!r} did NOT cause the production "
        f"helper to raise."
    )


# --- Invariant 4 wrong-fixtures (shared helper) ---


def _mutate_yaml_remove_v_gate_in_autopin(src: Path, dst: Path) -> None:
    """Remove the v* head_branch guard from auto-digest-bump.yml
    jobs.bump.if."""
    config = _load_workflow_yaml(src)
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
    dst.write_text(_dump_workflow_yaml(config))


def test_wrong_fixture_inv4_remove_v_gate_in_autopin(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: removing the v* gate breaks Invariant 4."""
    dst = fixture_dir / "auto-digest-bump.yml"
    _mutate_yaml_remove_v_gate_in_autopin(AUTO_PIN_WF, dst)
    try:
        assert_workflow_run_v_gate(dst)
    except WorkflowRunVGateError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: removing the v* head_branch "
        "gate did NOT cause the production helper to raise."
    )


# --- Invariant 5 wrong-fixtures ---


# Synthetic corrected guard block (the parent artifact)
# Synthetic corrected guard block (the parent artifact)
# Per Yua 21:21:24 #4 + 21:27:31 #2: align with the
# release-tag decision model. The corrected guard uses
# the same bounded SemVer 2.0.0 grammar as the Python
# resolver (Bash POSIX ERE equivalent: capturing groups
# instead of non-capturing groups).
CORRECTED_GUARD = (
    '          if ! [[ "$TAG" =~ ' + PROJECT_RELEASE_GRAMMAR_BASH + " ]]; then\n"
    '            echo "::error::Manual-dispatch tag must be a release tag (project release grammar)"\n'
    "            exit 1\n"
    "          fi\n"
)


def _write_corrected_resolve_step(src: Path, dst: Path) -> None:
    """Add the corrected guard to the Resolve step's run script.

    Per Yua 20:57:08 #1: the guard must appear AFTER all TAG
    sources resolve and BEFORE tag is emitted.
    """
    text = src.read_text(encoding="utf-8")
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
    """Move the guard to AFTER the tag output emission."""
    text = src.read_text(encoding="utf-8")
    text = text.replace(CORRECTED_GUARD, "")
    text = text.replace(
        'echo "tag=${TAG}" >> "$GITHUB_OUTPUT"',
        'echo "tag=${TAG}" >> "$GITHUB_OUTPUT"\n' + CORRECTED_GUARD,
    )
    dst.write_text(text, encoding="utf-8")


def _replace_guard_with_noop(src: Path, dst: Path) -> None:
    """Replace the guard with an always-pass predicate."""
    text = src.read_text(encoding="utf-8")
    noop_guard = """          if ! [[ "$TAG" == "" ]]; then
            echo "Always passes"
            exit 1
          fi
"""
    text = text.replace(CORRECTED_GUARD, noop_guard)
    dst.write_text(text, encoding="utf-8")


def _comment_only_guard(src: Path, dst: Path) -> None:
    """Replace the guard with a comment-only version."""
    text = src.read_text(encoding="utf-8")
    comment_guard = """          # if ! [[ "$TAG" == v* ]]; then
          #   echo "comment-only"
          #   exit 1
          # fi
"""
    text = text.replace(CORRECTED_GUARD, comment_guard)
    dst.write_text(text, encoding="utf-8")


def _inverted_guard(src: Path, dst: Path) -> None:
    """Replace the guard with an inverted version."""
    text = src.read_text(encoding="utf-8")
    inverted_guard = """          if [[ "$TAG" == v* ]]; then
            echo "Inverted: rejects v* tags"
            exit 1
          fi
"""
    text = text.replace(CORRECTED_GUARD, inverted_guard)
    dst.write_text(text, encoding="utf-8")


def _guard_outside_resolve_step(src: Path, dst: Path) -> None:
    """Remove the guard from Resolve and add a no-op step."""
    text = src.read_text(encoding="utf-8")
    text = text.replace(CORRECTED_GUARD, "")
    text = text.replace(
        "- name: Patch group_vars",
        "- name: Decoy guard step\n        run: |\n"
        + CORRECTED_GUARD
        + "\n      - name: Patch group_vars",
    )
    dst.write_text(text, encoding="utf-8")


def _exit_outside_if_block(src: Path, dst: Path) -> None:
    """Guard with exit OUTSIDE the if block (after fi)."""
    text = src.read_text(encoding="utf-8")
    outside_exit_guard = """          if ! [[ "$TAG" == v* ]]; then
            echo "Tag must start with v"
          fi
          exit 1
"""
    text = text.replace(CORRECTED_GUARD, outside_exit_guard)
    dst.write_text(text, encoding="utf-8")


# Synthetic corrected fixture (parent artifact)
def test_wrong_fixture_inv5_synthetic_fixed(fixture_dir: Any) -> None:
    """Synthetic corrected fixture: the guard is present and
    the contract is satisfied."""
    dst = fixture_dir / "auto-digest-bump.yml"
    _write_corrected_resolve_step(AUTO_PIN_WF, dst)
    assert_release_only_manual_dispatch_guard(dst)


def test_wrong_fixture_inv5_bypass_guard(fixture_dir: Any) -> None:
    """Wrong-fixture: bypass the guard (remove it)."""
    parent = fixture_dir / "parent.yml"
    child = fixture_dir / "auto-digest-bump.yml"
    _write_corrected_resolve_step(AUTO_PIN_WF, parent)
    _remove_guard_from_resolve_step(parent, child)
    try:
        assert_release_only_manual_dispatch_guard(child)
    except ManualDispatchGuardMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: bypassing the guard did NOT cause the production helper to raise."
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
        "output did NOT cause the production helper to raise."
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
        "noop did NOT cause the production helper to raise."
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
        "Wrong-fixture FAIL: comment-only guard did NOT cause the production helper to raise."
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
        "Wrong-fixture FAIL: inverted guard did NOT cause the production helper to raise."
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
        "did NOT cause the production helper to raise."
    )


def test_wrong_fixture_inv5_exit_outside_if_block(
    fixture_dir: Any,
) -> None:
    """Wrong-fixture: exit outside the if block (not inside)."""
    parent = fixture_dir / "parent.yml"
    child = fixture_dir / "auto-digest-bump.yml"
    _write_corrected_resolve_step(AUTO_PIN_WF, parent)
    _exit_outside_if_block(parent, child)
    try:
        assert_release_only_manual_dispatch_guard(child)
    except ManualDispatchGuardMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: exit outside the if block did "
        "NOT cause the production helper to raise."
    )


# --- Invariant 6 wrong-fixtures (shared helper) ---


def _mutate_yaml_overlap_enables(src: Path, dst: Path) -> None:
    """Add refs/heads/main to the semver enable (overlapping)."""
    config = _load_workflow_yaml(src)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                tags_value = tags_value.replace(
                    "startsWith(github.ref, 'refs/tags/v')",
                    "startsWith(github.ref, 'refs/tags/v') || github.ref == 'refs/heads/main'",
                )
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))


def test_wrong_fixture_inv6_overlap_enables(fixture_dir: Any) -> None:
    """Wrong-fixture: overlapping enables break Invariant 6."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_overlap_enables(PUBLISH_WF, dst)
    try:
        assert_release_channel_consumption(dst)
    except ChannelMetadataError:
        return
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: overlapping enables did NOT cause the production helper to raise."
    )


def _mutate_yaml_duplicate_prefix(src: Path, dst: Path) -> None:
    """Add a duplicate `prefix=v,prefix=v` to the semver rule."""
    config = _load_workflow_yaml(src)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                tags_value = tags_value.replace(
                    "prefix=v",
                    "prefix=v,prefix=v",
                )
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))


def test_wrong_fixture_inv2_duplicate_prefix(fixture_dir: Any) -> None:
    """Wrong-fixture: duplicate `prefix=v` is rejected.

    Per Yua 21:27:31 #1: detect duplicate keys BEFORE dict
    collapse. Reject a repeated key even when both values
    are identical.
    """
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_duplicate_prefix(PUBLISH_WF, dst)
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: duplicate prefix did NOT cause the production helper to raise."
    )


def _mutate_yaml_duplicate_enable(src: Path, dst: Path) -> None:
    """Add a duplicate enable to the raw rule."""
    config = _load_workflow_yaml(src)
    for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
        if "Derive image tags" in step.get("name", ""):
            with_block = step.get("with", {})
            tags_value = with_block.get("tags", "")
            if isinstance(tags_value, str):
                # Add a duplicate enable to the raw rule
                tags_value = tags_value.replace(
                    "enable=${{ github.event_name == 'workflow_dispatch' && github.event.inputs.tag != '' }}",
                    "enable=${{ github.event_name == 'workflow_dispatch' && github.event.inputs.tag != '' }},enable=${{ github.event_name == 'workflow_dispatch' && github.event.inputs.tag != '' }}",
                )
                with_block["tags"] = tags_value
            break
    dst.write_text(_dump_workflow_yaml(config))


def test_wrong_fixture_inv2_duplicate_enable(fixture_dir: Any) -> None:
    """Wrong-fixture: duplicate `enable=` is rejected.

    Per Yua 21:27:31 #1: detect duplicate keys BEFORE dict
    collapse. Reject a repeated key even when both values
    are identical.
    """
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_yaml_duplicate_enable(PUBLISH_WF, dst)
    try:
        assert_distinct_mutex_tags(dst)
    except MutexChannelMissingError:
        return
    raise InvariantError(
        "Wrong-fixture FAIL: duplicate enable did NOT cause the production helper to raise."
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
    assert _resolve_release_tag("v0.7.0", None) == "v0.7.0"


def test_control_blank_input_falls_back_to_latest_release() -> None:
    """Control 4: a blank input falls back to the latest release."""
    assert _resolve_release_tag("", "v1.13.0") == "v1.13.0"
    assert _resolve_release_tag("", "v0.7.0") == "v0.7.0"


def test_control_explicit_main_rejected() -> None:
    """Control 5: explicit 'main' input is rejected."""
    with pytest.raises(TagResolutionRejectError):
        _resolve_release_tag("main", "v1.13.0")


def test_control_malformed_v_prefix_rejected() -> None:
    """Control 6: malformed v-prefix values are rejected for
    both explicit and latest fallback.

    Per Yua 20:57:08 #6: use the release tag grammar
    RELEASE_TAG_GRAMMAR.
    """
    # Explicit malformed values
    for bad in ["v1garbage", "v", "v.", "v1.2", "v1.2.3.4", "1.13.0"]:
        with pytest.raises(TagResolutionRejectError):
            _resolve_release_tag(bad, None)
    # Latest fallback malformed values
    for bad in ["v1garbage", "v", "main", "", "1.13.0"]:
        with pytest.raises(TagResolutionRejectError):
            _resolve_release_tag("", bad)


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
            for step in config.get("jobs", {}).get("publish-core-image", {}).get("steps", []):
                if step.get("name", "") == "Sign the published image (keyless, via GitHub OIDC)":
                    step["if"] = "${{ github.event_name == 'workflow_dispatch' }}"
            if "on" in config and True not in config:
                config[True] = config.pop("on")
        pub_dst.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
        pub_hash = hashlib.sha256(pub_dst.read_bytes()).hexdigest()
        assert pub_hash != publish_hash_before
        assert hashlib.sha256(PUBLISH_WF.read_bytes()).hexdigest() == publish_hash_before
        assert hashlib.sha256(AUTO_PIN_WF.read_bytes()).hexdigest() == autopin_hash_before


# Network-capable modules that would let this contract suite call live
# GitHub / registries. stdlib ``subprocess`` is allowed (local bash
# harness only); anything that speaks HTTP must stay out.
_FORBIDDEN_NETWORK_MODULES = frozenset(
    {
        "requests",
        "httpx",
        "urllib",
        "urllib.request",
        "urllib3",
        "aiohttp",
        "http.client",
        "github",
    }
)


def test_control_no_live_github_actions_called() -> None:
    """Control 11: this suite must not import network-capable modules.

    Scans the *entire* module AST (module-level imports AND function
    bodies). A top-level ``import requests`` / ``from urllib.request
    import urlopen`` must fail this control — not only imports nested
    inside a function.
    """
    import ast

    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=__file__)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if alias.name in _FORBIDDEN_NETWORK_MODULES or root in _FORBIDDEN_NETWORK_MODULES:
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            full = node.module
            root = full.split(".", 1)[0]
            if full in _FORBIDDEN_NETWORK_MODULES or root in _FORBIDDEN_NETWORK_MODULES:
                offenders.append(full)
    assert not offenders, (
        "Release-automation contract suite must not import network-capable "
        f"modules (would enable live GitHub/registry calls): {offenders!r}"
    )


def test_control_autopin_workflow_absence_fails_hard(tmp_path: Path) -> None:
    """Control 12: missing auto-digest-bump.yml must FAIL, never skip.

    Contract B / Inv4 are MUSTs. Deletion of the workflow fixture is a
    red condition, not a silent pass via ``pytest.skip``.
    """
    missing = tmp_path / "auto-digest-bump.yml"
    assert not missing.exists()
    with pytest.raises((FileNotFoundError, InvariantError, WorkflowRunVGateError, OSError)):
        assert_workflow_run_v_gate(missing)
    # Positive control still requires the real file to exist.
    assert AUTO_PIN_WF.exists(), (
        "auto-digest-bump.yml is a MUST fixture for Contract B / Inv4; "
        "absence must fail the suite, not skip"
    )
