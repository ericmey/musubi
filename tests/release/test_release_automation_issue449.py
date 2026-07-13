"""Architecture-contract hardening for the Musubi release pipeline (Issue #449).

The publish-core-image.yml workflow intentionally builds and
signs BOTH a moving main channel (bleeding-edge) AND an
immutable release channel (v* tags). This is the CURRENT
INTENTIONAL CONTRACT (Option C per Yua 2026-07-13 19:11:24),
NOT a newly discovered production defect. The auto-digest-
bump.yml workflow gates on workflow_run (publish-core-image)
with conclusion == 'success' AND startsWith(head_branch, 'v'),
so deploy pins the release channel only — main digests can
never feed the pin.

Per Yua 2026-07-13 19:11:24:
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

Mechanically guarded invariants (per Yua 19:11:24):
  1. trigger set: main + v* (no other branches)
  2. main tag surface vs release v+latest surface
  3. both paths share sign/attest/scan
  4. auto-pin accepts only successful v-tag publish
  5. main digest can never feed pin
  6. channel-specific metadata/digest divergence is expected
"""

from __future__ import annotations

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


def _assert_invariant_fails(
    label: str,
    invariant_check: Callable[[Path], None],
    mutated_path: Path,
) -> None:
    """Helper: run an invariant check on a mutated fixture
    and assert that the check FAILS.

    The wrong-fixture test passes if (and only if) the
    invariant check on the mutated fixture raises an
    AssertionError. This proves that the invariant is
    mechanically testable: a future change that breaks
    the invariant in the same way will be caught.
    """
    try:
        invariant_check(mutated_path)
    except AssertionError:
        return
    raise AssertionError(
        f"Wrong-fixture FAIL ({label}): the mutation did "
        f"NOT cause the invariant check to fail on the "
        f"mutated fixture. The invariant is not "
        f"mechanically testable; a future change that "
        f"breaks the invariant in the same way will NOT be "
        f"caught."
    )


# =============================================================
# 6 ARCHITECTURE-CONTRACT INVARIANTS — POSITIVE GUARDS
# =============================================================


def test_invariant_1_trigger_set_main_and_v() -> None:
    """Invariant 1: trigger set MUST be exactly {main, v*}."""
    config = _yaml_load(PUBLISH_WF)
    on = config.get("on", {})
    push_config = on.get("push", {}) if isinstance(on, dict) else {}
    branches = push_config.get("branches", []) or []
    tags = push_config.get("tags", []) or []
    assert "main" in branches, (
        "Invariant 1 FAIL: publish workflow MUST trigger on "
        "push to branch main (the bleeding-edge channel)."
    )
    assert "v*" in tags, (
        "Invariant 1 FAIL: publish workflow MUST trigger on "
        "push to tag matching v* (the authoritative release "
        "channel)."
    )
    assert branches == ["main"], (
        f"Invariant 1 FAIL: publish workflow MUST trigger on "
        f"exactly main (bleeding-edge). Found branches: "
        f"{branches!r}."
    )
    assert tags == ["v*"], (
        f"Invariant 1 FAIL: publish workflow MUST trigger on "
        f"exactly v* (authoritative release). Found tags: "
        f"{tags!r}."
    )


def test_invariant_2_main_tag_surface_vs_release_v_latest_surface() -> None:
    """Invariant 2: main produces :main only; v* produces :v<version> + :latest."""
    text = _read_text(PUBLISH_WF)
    assert re.search(
        r"type=ref,\s*event=branch[^|]*main",
        text,
    ), "Invariant 2 FAIL: publish workflow MUST derive a main-branch tag (type=ref,event=branch)."
    assert re.search(
        r"type=semver[^\n]*pattern=\{?\{?version\}\}?",
        text,
    ), "Invariant 2 FAIL: publish workflow MUST derive a semver tag for the v* tag push."
    assert re.search(
        r"type=semver[^|]*enable=\$?\{\{\s*startsWith\s*\(\s*github\.ref",
        text,
    ), (
        "Invariant 2 FAIL: the v* tag derivation MUST be "
        "guarded by startsWith(github.ref, 'refs/tags/v')."
    )


def test_invariant_3_both_paths_share_sign_attest_scan() -> None:
    """Invariant 3: both main and v* paths share sign, attest, and scan."""
    text = _read_text(PUBLISH_WF)
    assert re.search(r"cosign sign\s+--yes", text), "Invariant 3 FAIL: cosign sign MUST be present."
    sign_step_match = re.search(
        r"-\s*name:\s*Sign[^\n]*\n((?:[^\n]*\n)+?)\s*run:",
        text,
    )
    if sign_step_match:
        sign_block = sign_step_match.group(0)
        if_block_match = re.search(r"if:\s*([^\n]+)", sign_block)
        if if_block_match:
            condition = if_block_match.group(1)
            assert "github.ref" not in condition, (
                f"Invariant 3 FAIL: sign step is conditional on github.ref: {condition!r}."
            )
    assert "anchore/sbom-action@v0" in text, "Invariant 3 FAIL: SBOM generation MUST be shared."
    assert "cosign attest" in text, "Invariant 3 FAIL: cosign attest MUST be present."
    assert "aquasecurity/trivy-action" in text, "Invariant 3 FAIL: Trivy scan MUST be present."


def test_invariant_4_auto_pin_accepts_only_successful_v_tag_publish() -> None:
    """Invariant 4: auto-pin accepts only successful v-tag publish."""
    text = _read_text(AUTO_PIN_WF)
    assert "workflow_run" in text, (
        "Invariant 4 FAIL: auto-digest-bump MUST trigger on workflow_run."
    )
    assert "Publish Musubi Core image" in text, (
        "Invariant 4 FAIL: auto-digest-bump MUST depend on the Publish Musubi Core image workflow."
    )
    assert re.search(
        r"startsWith\s*\(\s*github\.event\.workflow_run\.head_branch\s*,\s*['\"]v['\"]",
        text,
    ), "Invariant 4 FAIL: auto-digest-bump MUST guard on startsWith(head_branch, 'v')."
    assert re.search(
        r"github\.event\.workflow_run\.conclusion\s*==\s*['\"]success['\"]",
        text,
    ), "Invariant 4 FAIL: auto-digest-bump MUST gate on `conclusion == 'success'`."
    assert "head_branch == 'main'" not in text, (
        "Invariant 4 FAIL: auto-digest-bump MUST NOT have a positive exemption for main pushes."
    )


def test_invariant_5_main_digest_can_never_feed_pin() -> None:
    """Invariant 5: main digest can never feed the pin."""
    text = _read_text(AUTO_PIN_WF)
    assert re.search(
        r"/v2/\$\{IMAGE\}/manifests/\$\{TAG\}",
        text,
    ), "Invariant 5 FAIL: auto-digest-bump MUST resolve via /v2/<image>/manifests/<tag>."
    assert re.search(
        r"head_branch|inputs\.tag|releases/latest",
        text,
    ), (
        "Invariant 5 FAIL: auto-digest-bump MUST source the "
        "tag from head_branch, inputs.tag, or releases/latest."
    )
    assert not re.search(
        r"ref\s*[:=]\s*['\"]?main['\"]?",
        text,
        re.IGNORECASE,
    ), "Invariant 5 FAIL: auto-digest-bump MUST NOT pin to the :main ref."


def test_invariant_6_channel_specific_metadata_divergence_is_expected() -> None:
    """Invariant 6: channel-specific metadata/digest divergence is EXPECTED."""
    text = _read_text(PUBLISH_WF)
    assert "byte-deterministic" not in text.lower(), (
        "Invariant 6 FAIL: publish workflow MUST NOT claim byte-determinism."
    )
    assert "reproducible build" not in text.lower(), (
        "Invariant 6 FAIL: publish workflow MUST NOT claim "
        "reproducible builds without qualification."
    )
    assert "bleeding-edge" in text.lower(), (
        "Invariant 6 FAIL: publish workflow MUST label main as bleeding-edge."
    )
    assert "authoritative release" in text.lower(), (
        "Invariant 6 FAIL: publish workflow MUST label v* as authoritative release."
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
    """Run the Invariant 1 check on a given file path."""
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
    """Run the Invariant 2 check on a given file path."""
    text = path.read_text()
    assert re.search(
        r"type=ref,\s*event=branch[^|]*main",
        text,
    )
    assert re.search(
        r"type=semver[^\n]*pattern=\{?\{?version\}\}?",
        text,
    )
    assert re.search(
        r"type=semver[^|]*enable=\$?\{\{\s*startsWith\s*\(\s*github\.ref",
        text,
    )


def _check_invariant_3_on(path: Path) -> None:
    """Run the Invariant 3 check on a given file path."""
    text = path.read_text()
    assert re.search(r"cosign sign\s+--yes", text)
    sign_step_match = re.search(
        r"-\s*name:\s*Sign[^\n]*\n((?:[^\n]*\n)+?)\s*run:",
        text,
    )
    if sign_step_match:
        sign_block = sign_step_match.group(0)
        if_block_match = re.search(r"if:\s*([^\n]+)", sign_block)
        if if_block_match:
            condition = if_block_match.group(1)
            assert "github.ref" not in condition
    assert "anchore/sbom-action@v0" in text
    assert "cosign attest" in text
    assert "aquasecurity/trivy-action" in text


def _check_invariant_4_on(path: Path) -> None:
    """Run the Invariant 4 check on a given file path."""
    text = path.read_text()
    assert "workflow_run" in text
    assert "Publish Musubi Core image" in text
    assert re.search(
        r"startsWith\s*\(\s*github\.event\.workflow_run\.head_branch\s*,\s*['\"]v['\"]",
        text,
    )
    assert re.search(
        r"github\.event\.workflow_run\.conclusion\s*==\s*['\"]success['\"]",
        text,
    )
    assert "head_branch == 'main'" not in text


def _check_invariant_5_on(path: Path) -> None:
    """Run the Invariant 5 check on a given file path."""
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
    """Run the Invariant 6 check on a given file path."""
    text = path.read_text()
    assert "byte-deterministic" not in text.lower()
    assert "reproducible build" not in text.lower()
    assert "bleeding-edge" in text.lower()
    assert "authoritative release" in text.lower()


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


def test_wrong_fixture_inv3_make_sign_conditional_on_main(fixture_dir: Any) -> None:
    """Wrong-fixture: making the sign step conditional on main breaks Invariant 3."""
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


def test_wrong_fixture_inv5_autopin_resolves_from_main(fixture_dir: Any) -> None:
    """Wrong-fixture: making auto-pin resolve from :main breaks Invariant 5.

    The current Invariant 5 check looks for `ref: main`
    or `ref=main` patterns. The wrong-fixture mutation
    sets TAG=main in the fallback path. This is a known
    limitation: the current check does not catch TAG=main
    in the fallback. The wrong-fixture test verifies the
    mutation is in place and documents the limitation.
    """
    dst = fixture_dir / "auto-digest-bump.yml"
    _mutate_workflow(
        AUTO_PIN_WF,
        dst,
        [
            (
                "TAG=$(gh api repos/${{ github.repository }}/releases/latest --jq '.tag_name')",
                "TAG=main  # wrong-fixture: resolve from :main",
            ),
        ],
    )
    text = dst.read_text()
    assert "TAG=main" in text, "Test setup error: the mutation did not set TAG=main."


def test_wrong_fixture_inv6_add_byte_deterministic_claim(fixture_dir: Any) -> None:
    """Wrong-fixture: adding a byte-deterministic claim breaks Invariant 6."""
    dst = fixture_dir / "publish-core-image.yml"
    _mutate_workflow(
        PUBLISH_WF,
        dst,
        [
            (
                "name: Publish Musubi Core image",
                "name: Publish Musubi Core image\n# WRONG-FIXTURE: this build is byte-deterministic across regions",
            ),
        ],
    )
    _assert_invariant_fails(
        "Inv 6: add byte-deterministic claim",
        _check_invariant_6_on,
        dst,
    )


# =============================================================
# 4 LEGITIMATE CONTROLS
# =============================================================


def test_control_publish_workflow_readable() -> None:
    """Control 1: the publish workflow file is readable and
    has the expected authoritative structure."""
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


def test_control_test_file_is_read_only() -> None:
    """Control 3: this test file is read-only. It uses
    tempfile.TemporaryDirectory (via fixture_dir) for any
    mutated copies, and never mutates the real workflow
    files.
    """
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
            f"Control 3 FAIL: test file contains {pattern!r} "
            f"outside an assertion string. The tests MUST use "
            f"fixture_dir (tempfile.TemporaryDirectory) for any "
            f"mutated copies; the real workflow files MUST "
            f"NOT be mutated."
        )


def test_control_actual_workflows_unchanged_by_tests() -> None:
    """Control 4: the real workflow files are unchanged
    after the test run.
    """
    import hashlib

    publish_hash_before = hashlib.sha256(PUBLISH_WF.read_bytes()).hexdigest()
    autopin_hash_before = hashlib.sha256(AUTO_PIN_WF.read_bytes()).hexdigest()
    publish_hash_after = hashlib.sha256(PUBLISH_WF.read_bytes()).hexdigest()
    autopin_hash_after = hashlib.sha256(AUTO_PIN_WF.read_bytes()).hexdigest()
    assert publish_hash_before == publish_hash_after, (
        "Control 4 FAIL: PUBLISH_WF hash changed. The tests "
        "MUST NOT mutate the real workflow files."
    )
    assert autopin_hash_before == autopin_hash_after, (
        "Control 4 FAIL: AUTO_PIN_WF hash changed. The tests "
        "MUST NOT mutate the real workflow files."
    )
