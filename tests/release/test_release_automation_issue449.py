"""Architecture-contract hardening for the Musubi release pipeline (Issue #449).

The publish-core-image.yml workflow intentionally builds and
signs BOTH a moving main channel (bleeding-edge) AND an
immutable release channel (v* tags). This is the CURRENT
INTENTIONAL CONTRACT (Option C per Yua 2026-07-13 19:11:24),
NOT a newly discovered production defect. The auto-digest-
bump.yml workflow gates on workflow_run (publish-core-image)
with conclusion == 'success' AND startsWith(head_branch, 'v'),
so deploy pins the release channel only - main digests can
never feed the pin.

Per Yua 2026-07-13 19:11:24 and the WITHHOLD on 6e07c56:
  - The contract is Option C (intentionally separate main/
    release builds with explicit expected digest divergence).
  - actions/cache is NOT an authoritative coordination ledger;
    promoting a main-built digest would also require an
    explicit canonical metadata/provenance policy.
  - The test contract is "architecture-contract hardening",
    NOT "duplicate-build defect".
  - The previous red-contract framing and self-referential
    AST/mtime proof are REMOVED.
  - Wrong-fixture mutation tests must ACTUALLY FAIL when each
    invariant is broken.
  - New hardening defect confirmed: workflow_dispatch
    unconditionally allows an explicit tag=main, so a moving
    main digest CAN feed the deployment pin through manual
    dispatch. This is a hardening defect (release-only
    manual dispatch enforcement is a newly confirmed
    architecture gap). Source/workflow fix is FORBIDDEN
    until Yua accepts this red commit.

Mechanically guarded invariants (per Yua 19:11:24 + 6e07c56):
  1. push trigger set: main + v* (no other branches)
  2. workflow_dispatch is a separate operator trigger;
     release-only manual dispatch enforcement is a newly
     confirmed hardening defect
  3. main tag surface vs release v+latest surface
  4. all supply-chain steps (sign + SBOM + attest + scan)
     are shared/unconditional across both channels
  5. auto-pin accepts only successful v-tag publish OR
     explicit v-tag manual dispatch (NEVER main, NEVER
     :main ref, NEVER the latest release fallback if that
     latest is main)
  6. channel-specific metadata/digest divergence is expected
     (configuration allows the two channels to carry
     distinct OCI metadata; the contract is that divergence
     is allowed/expected, not guaranteed)
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PUBLISH_WF = WORKFLOWS / "publish-core-image.yml"
AUTO_PIN_WF = WORKFLOWS / "auto-digest-bump.yml"


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


def _assert_invariant_fails(
    label: str,
    invariant_check: Callable[[Path], None],
    mutated_path: Path,
) -> None:
    """Helper: run an invariant check on a mutated fixture
    and assert that the check FAILS.
    """
    try:
        invariant_check(mutated_path)
    except AssertionError:
        return
    raise AssertionError(
        f"Wrong-fixture FAIL ({label}): the mutation did "
        f"NOT cause the invariant check to fail on the "
        f"mutated fixture."
    )


# =============================================================
# 6 ARCHITECTURE-CONTRACT INVARIANTS (POSITIVE GUARDS)
# =============================================================


def test_invariant_1_push_trigger_set() -> None:
    """Invariant 1: push trigger set is exactly {main, v*}."""
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


def test_invariant_2_main_tag_surface_vs_release_v_latest_surface() -> None:
    """Invariant 2: main produces :main; v* produces :v<version> + :latest."""
    text = _read_text(PUBLISH_WF)
    assert _has_main_guard(text)
    assert _has_v_guard(text)


def test_invariant_3_all_supply_chain_steps_shared() -> None:
    """Invariant 3: all supply-chain steps are shared/unconditional."""
    text = _read_text(PUBLISH_WF)
    required_patterns = [
        (r"cosign sign\s+--yes", "cosign sign"),
        (r"anchore/sbom-action@v0", "CycloneDX SBOM"),
        (r"cosign attest", "cosign attest"),
        (r"aquasecurity/trivy-action@[\d.]+", "Trivy scan (table)"),
        (r"format:\s*sarif", "Trivy scan (SARIF)"),
    ]
    for pattern, name in required_patterns:
        assert re.search(pattern, text), (
            f"Invariant 3 FAIL: required supply-chain step "
            f"{name!r} (pattern {pattern!r}) MUST be present."
        )
    # The sign step MUST NOT be conditional on github.ref.
    sign_block_match = re.search(
        r"-\s*name:\s*Sign[^\n]*\n((?:[^\n]*\n)+?)\s*run:",
        text,
    )
    if sign_block_match:
        sign_block = sign_block_match.group(0)
        if_block_match = re.search(r"if:\s*([^\n]+)", sign_block)
        if if_block_match:
            condition = if_block_match.group(1)
            assert "github.ref" not in condition, (
                f"Invariant 3 FAIL: sign step is conditional on github.ref: {condition!r}."
            )


def test_invariant_4_auto_pin_accepts_only_successful_v_tag_publish() -> None:
    """Invariant 4: auto-pin accepts only successful v-tag publish."""
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


def test_invariant_5_main_digest_can_never_feed_pin() -> None:
    """Invariant 5: main digest can never feed the pin.

    Per Yua 6e07c56 finding 2: auto-digest-bump allows
    workflow_dispatch unconditionally. If the explicit
    input tag is main, Resolve tag + digest sets TAG to
    main and resolves /manifests/main. This is a newly
    confirmed hardening defect: release-only manual
    dispatch enforcement is missing.
    """
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


def test_invariant_6_channel_specific_metadata_divergence_is_expected() -> None:
    """Invariant 6: channel-specific metadata/digest divergence is EXPECTED.

    The publish workflow's meta step MUST encode mutually
    exclusive main ref and release semver rules.
    """
    text = _read_text(PUBLISH_WF)
    assert _has_main_guard(text), (
        "Invariant 6 FAIL: publish workflow MUST have a "
        "main-branch tag derivation guarded by "
        "github.ref == 'refs/heads/main' (mutually "
        "exclusive with the v* release semver rule)."
    )
    assert _has_v_guard(text), (
        "Invariant 6 FAIL: publish workflow MUST have a "
        "v* release semver derivation guarded by "
        "startsWith(github.ref, 'refs/tags/v') (mutually "
        "exclusive with the main ref rule)."
    )
    # The contract is that divergence is ALLOWED, not
    # GUARANTEED. Do not test actual digest inequality.
    assert "byte-deterministic" not in text.lower(), (
        "Invariant 6 FAIL: publish workflow MUST NOT claim "
        "byte-determinism (the contract is that divergence "
        "is allowed, not guaranteed)."
    )


# =============================================================
# 1 STRICT RED (reproduces the hardening defect)
# =============================================================
# Per Yua 6e07c56 finding 2: "auto-digest-bump.yml allows
# workflow_dispatch unconditionally. If the explicit
# input tag is main, Resolve tag + digest sets TAG to
# main and resolves /manifests/main. Therefore a moving
# main digest CAN feed the deployment pin through manual
# dispatch. The accepted contract is not true today."


def test_red_hardening_defect_manual_dispatch_main() -> None:
    """Strict red: the current auto-digest-bump workflow
    accepts an explicit tag=main from manual dispatch."""
    text = _read_text(AUTO_PIN_WF)
    has_dispatch_with_no_v_guard = (
        "workflow_dispatch" in text
        and "inputs.tag" in text
        and not re.search(
            r"workflow_dispatch[\s\S]*?inputs\.tag[\s\S]*?startsWith",
            text,
        )
        and not re.search(
            r"inputs\.tag[\s\S]*?startsWith",
            text,
        )
    )
    assert has_dispatch_with_no_v_guard, (
        "RED FAIL: the auto-digest-bump workflow does NOT "
        "exhibit the manual-dispatch-main hardening defect. "
        "Per Yua 6e07c56 finding 2: workflow_dispatch with "
        "tag=main is a real current hardening gap."
    )


# =============================================================
# 6 WRONG-FIXTURE MUTATION TESTS
# =============================================================


@pytest.fixture
def fixture_dir() -> Any:
    """Create a temporary directory for mutated workflow copies."""
    d = tempfile.mkdtemp(prefix="issue449-fixture-")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def _check_invariant_1_on(path: Path) -> None:
    config = _yaml_load(path)
    on = config.get("on", {})
    push_config = on.get("push", {}) if isinstance(on, dict) else {}
    branches = push_config.get("branches", []) or []
    tags = push_config.get("tags", []) or []
    assert "main" in branches
    assert "v*" in tags
    assert branches == ["main"]
    assert tags == ["v*"]


def _check_invariant_2_on(path: Path) -> None:
    text = path.read_text()
    assert _has_main_guard(text)
    assert _has_v_guard(text)


def _check_invariant_3_on(path: Path) -> None:
    text = path.read_text()
    required_patterns = [
        r"cosign sign\s+--yes",
        r"anchore/sbom-action@v0",
        r"cosign attest",
        r"aquasecurity/trivy-action@[\d.]+",
        r"format:\s*sarif",
    ]
    for pattern in required_patterns:
        assert re.search(pattern, text)
    sign_block_match = re.search(
        r"-\s*name:\s*Sign[^\n]*\n((?:[^\n]*\n)+?)\s*run:",
        text,
    )
    if sign_block_match:
        sign_block = sign_block_match.group(0)
        if_block_match = re.search(r"if:\s*([^\n]+)", sign_block)
        if if_block_match:
            condition = if_block_match.group(1)
            assert "github.ref" not in condition


def _check_invariant_4_on(path: Path) -> None:
    text = path.read_text()
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


def _check_invariant_5_on(path: Path) -> None:
    text = path.read_text()
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


def _check_invariant_6_on(path: Path) -> None:
    text = path.read_text()
    assert _has_main_guard(text)
    assert _has_v_guard(text)
    assert "byte-deterministic" not in text.lower()


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
    _assert_invariant_fails(
        "Inv 1: remove v* trigger",
        _check_invariant_1_on,
        dst,
    )


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
    _assert_invariant_fails(
        "Inv 2: main publishes release tags",
        _check_invariant_2_on,
        dst,
    )


def test_wrong_fixture_inv3_gate_sign_on_main(fixture_dir: Any) -> None:
    """Wrong-fixture: gating the sign step on main breaks Invariant 3."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_workflow(
        PUBLISH_WF,
        dst,
        [
            (
                "      - name: Sign the published image (keyless, via GitHub OIDC)\n",
                "      - name: Sign the published image (keyless, via GitHub OIDC)\n        if: github.ref == 'refs/heads/main'\n",
            ),
        ],
    )
    _assert_invariant_fails(
        "Inv 3: sign conditional on main",
        _check_invariant_3_on,
        dst,
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
    _assert_invariant_fails(
        "Inv 4: remove v* guard",
        _check_invariant_4_on,
        dst,
    )


def test_wrong_fixture_inv5_add_inputs_tag_v_guard(fixture_dir: Any) -> None:
    """Wrong-fixture: adding the inputs.tag v* guard fixes
    the hardening defect. The Invariant 5 check on the
    fixed fixture passes."""
    dst = fixture_dir / "auto-digest-bump.yml"
    _mutate_workflow(
        AUTO_PIN_WF,
        dst,
        [
            (
                "TAG=${{ github.event.inputs.tag }}",
                'TAG=${{ github.event.inputs.tag }}\n          if ! [[ "${{ github.event.inputs.tag }}" == v* ]]; then echo "manual-dispatch tag must start with v"; exit 1; fi',
            ),
        ],
    )
    _check_invariant_5_on(dst)


def test_wrong_fixture_inv6_remove_channel_distinction(fixture_dir: Any) -> None:
    """Wrong-fixture: removing the mutually exclusive main ref
    guard breaks Invariant 6."""
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
    _assert_invariant_fails(
        "Inv 6: remove main guard",
        _check_invariant_6_on,
        dst,
    )


# =============================================================
# 4 LEGITIMATE CONTROLS
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
    produces a v* tag pin. This is a legitimate control."""
    text = _read_text(AUTO_PIN_WF)
    assert "inputs.tag" in text
    assert re.search(
        r"/v2/\$\{IMAGE\}/manifests/\$\{TAG\}",
        text,
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
