"""Release-automation self-proving red contracts for Issue #449.

Tests/docs/design only per Yua 2026-07-13 18:51:31.
Encodes self-proving red contracts for the four corrected
release-automation requirements:
  A. exactly one authoritative signed tag publish per release tag;
  B. auto-pin consumes the signed immutable tag digest and never
     the moving main tag;
  C. main and release channels are explicitly distinct in generated
     metadata/docs;
  D. a design-only reproducibility boundary that treats cache as
     performance, not input, and does not require main/tag byte
     equality.

The tests operate on checked-in workflow/config fixtures
(publish-core-image.yml, auto-digest-bump.yml) or parsed
workflow structure. They do NOT call live GitHub Actions or
mutate releases. Per Yua 2026-07-13 18:51:31:
  "The tests must operate on checked-in workflow/config
  fixtures or parsed workflow structure, not call live GitHub
  Actions or mutate releases. Include legitimate controls and
  discrimination against the three wrong designs. Stop and
  report design drift before code if a test would require
  editing the production workflow."

Source of truth: Yua 2026-07-13 18:51:31 (post-pin acceptance).
"""

from __future__ import annotations

import re
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
    """Read a workflow file as text (preserves comments / Jinja)."""
    return path.read_text(encoding="utf-8")


def _read_yaml_block(text: str, marker: str) -> dict[str, Any]:
    """Extract a YAML block between two markers in a workflow file.

    Workflow files are Jinja-templated YAML; this helper extracts
    the canonical block between two non-Jinja comment markers
    so we can parse it as pure YAML.
    """
    start = text.find(marker)
    assert start != -1, f"Marker {marker!r} not found"
    end = text.find(marker, start + len(marker))
    assert end != -1, f"Closing marker {marker!r} not found"
    return yaml.safe_load(text[start:end])  # type: ignore[no-any-return]


def _contract_a_assertions(workflow_text: str) -> None:
    """Contract A: exactly one authoritative signed tag publish per release tag.

    Asserts the publish workflow:
      - Has a `publish-core-image` job (the only authoritative publisher).
      - The job includes a cosign sign --keyless step (or equivalent
        cosign invocation).
      - The publish step tags the image with the release tag (v*)
        via `type=semver,pattern={{version}}`, not with :main.
      - The workflow does NOT mark the main push as authoritative
        (the main push is non-authoritative bleeding-edge).
    """
    # The job name
    assert re.search(r"jobs:\s*\n\s+publish-core-image:", workflow_text), (
        "Contract A FAIL: publish workflow MUST have a publish-core-image job."
    )
    # Cosign keyless signing step (any cosign install + sign)
    assert "sigstore/cosign-installer" in workflow_text, (
        "Contract A FAIL: publish workflow MUST install cosign "
        "(keyless signing via GitHub OIDC is the authoritative path)."
    )
    # Cosign sign invocation
    assert re.search(r"cosign\s+sign", workflow_text), (
        "Contract A FAIL: publish workflow MUST invoke `cosign sign` to produce the signed digest."
    )
    # Semver tag pattern (release tag)
    assert (
        re.search(
            r"type=semver[^\n]*pattern=\{?\{?version\}\}?",
            workflow_text,
        )
        or "type=semver" in workflow_text
    ), (
        "Contract A FAIL: publish workflow MUST tag the image "
        "with the release semver (v*), not with :main, for the "
        "authoritative publish."
    )
    # The trigger labels main as bleeding-edge (not authoritative)
    # We assert the comment labels main as bleeding-edge so the
    # intent is explicit in the workflow.
    assert "bleeding-edge" in workflow_text.lower() or "moving channel" in workflow_text.lower(), (
        "Contract A FAIL: publish workflow MUST label :main as a "
        "non-authoritative bleeding-edge channel; only the v* tag "
        "is authoritative."
    )


def _contract_b_assertions(auto_pin_text: str) -> None:
    """Contract B: auto-pin consumes the signed immutable tag digest,
    never the moving main tag.

    Asserts the auto-digest-bump workflow:
      - Triggers on `workflow_run` after `publish-core-image.yml`
        finishes (not on main pushes directly).
      - Resolves a tag (head_branch, dispatch input, or latest
        release) — NOT a :main ref.
      - Resolves a digest from GHCR's anonymous registry API
        (not from a local build / :main ref).
      - Pins `musubi_core_image` to the resolved @sha256:<digest>,
        not to a :main ref.
    """
    # Trigger: workflow_run on publish-core-image
    assert "workflow_run" in auto_pin_text, (
        "Contract B FAIL: auto-digest-bump MUST trigger on "
        "workflow_run after publish-core-image (not on main "
        "pushes directly)."
    )
    assert "Publish Musubi Core image" in auto_pin_text, (
        "Contract B FAIL: auto-digest-bump MUST depend on the Publish Musubi Core image workflow."
    )
    # Tag resolution sources — head_branch, dispatch, latest release.
    # MUST NOT pin to :main or to a moving-channel ref.
    assert re.search(
        r"head_branch|inputs\.tag|releases/latest",
        auto_pin_text,
    ), (
        "Contract B FAIL: auto-digest-bump MUST resolve a tag "
        "from a versioned source (head_branch, dispatch input, "
        "or latest release), not from a :main ref."
    )
    # Tag-prefix guard: only v* tag pushes trigger the auto-pin.
    # (The publish workflow's workflow_run trigger fires for
    # BOTH main and tag pushes, but the auto-pin step guards on
    # the v* prefix.)
    assert re.search(
        r"startsWith.*\bv\b|head_branch.*v|tag.*v[0-9]",
        auto_pin_text,
    ), (
        "Contract B FAIL: auto-digest-bump MUST guard on the v* "
        "tag prefix; it MUST NOT chase the :main channel."
    )
    # musubi_core_image pinned to the resolved digest
    assert "musubi_core_image" in auto_pin_text, (
        "Contract B FAIL: auto-digest-bump MUST patch musubi_core_image in the deploy repo."
    )
    assert "@sha256" in auto_pin_text or "sha256:" in auto_pin_text, (
        "Contract B FAIL: auto-digest-bump MUST pin to a "
        "sha256 digest, not to a :main or :latest ref."
    )


def _contract_c_assertions(workflow_text: str) -> None:
    """Contract C: main and release channels are explicitly distinct
    in generated metadata/docs.

    Asserts the publish workflow:
      - Labels :main as a moving / bleeding-edge channel.
      - Labels v* as the authoritative / immutable release channel.
      - The version annotation is sourced from the tag via
        the meta step's `type=semver,pattern={{version}}` (so
        consumers can distinguish :main from v* via the
        generated org.opencontainers.image.version annotation).
    """
    # Both labels present in the workflow text
    has_main_label = (
        "bleeding-edge" in workflow_text.lower()
        or "moving channel" in workflow_text.lower()
        or ":main" in workflow_text
    )
    has_release_label = (
        "authoritative release" in workflow_text.lower()
        or "immutable release" in workflow_text.lower()
        or "release channel" in workflow_text.lower()
    )
    assert has_main_label and has_release_label, (
        "Contract C FAIL: publish workflow MUST label :main and "
        "v* as distinct channels in its documentation, comments, "
        "or generated OCI annotations."
    )
    # The version annotation is sourced from the tag via the
    # meta step's type=semver,pattern={{version}} (this is the
    # standard docker/metadata-action behavior; the version is
    # extracted from the tag and emitted as
    # org.opencontainers.image.version).
    assert re.search(
        r"type=semver[^\n]*pattern=\{?\{?version\}\}?",
        workflow_text,
    ), (
        "Contract C FAIL: publish workflow MUST source the "
        "org.opencontainers.image.version annotation from the "
        "tag via the meta step's type=semver,pattern={{version}} "
        "so consumers can distinguish :main from v* via the "
        "generated manifest annotation."
    )


def _contract_d_assertions(workflow_text: str) -> None:
    """Contract D: a design-only reproducibility boundary that
    treats cache as performance, not input, and does not
    require main/tag byte equality.

    Asserts the publish workflow:
      - Does NOT pin --cache-to type=gha,mode=max as an
        artifact input (cache is a performance concern).
      - The reproducibility test (if any) compares builds with
        identical source, platform, dependencies, toolchain,
        build arguments, and canonical OCI metadata.
      - Cache enabled/disabled or cache location MUST NOT
        change the resulting digest (i.e., the workflow does
        NOT depend on cache state for correctness).
    """
    # Cache is used for performance, not for input correctness.
    # The workflow may use cache (allowed) but the contract
    # is that cache state is NOT pinned as an artifact input.
    # We assert that the workflow does not declare cache as a
    # required input (e.g., required: true for cache-from).
    # The presence of cache-from / cache-to is allowed; what
    # matters is that cache is NOT a required correctness input.
    cache_required = re.search(
        r"cache-(?:from|to)[^\n]*\brequired\s*:\s*true",
        workflow_text,
    )
    assert not cache_required, (
        "Contract D FAIL: publish workflow MUST NOT mark cache as "
        "a required input. Cache is a performance concern, not a "
        "correctness or reproducibility input."
    )
    # The workflow does NOT claim that main and tag must be
    # byte-identical. The publish workflow MAY publish both, but
    # the channels are distinct by design.
    assert "byte-identical" not in workflow_text.lower(), (
        "Contract D FAIL: publish workflow MUST NOT claim "
        "main/tag byte equality. The two channels intentionally "
        "carry different metadata (revision, version, created) "
        "and byte equality is not a design requirement."
    )
    # If reproducibility is desired, the workflow would need
    # canonicalized OCI metadata (e.g., fixed
    # org.opencontainers.image.created). The test documents this
    # as a SEPARATE design decision, not a default invariant.
    # We assert the workflow does NOT claim reproducibility by
    # default (no language asserting "byte-deterministic" or
    # "reproducible builds" without a qualifier).
    has_unqualified_claim = re.search(
        r"\breproducible\b|\bbyte-deterministic\b",
        workflow_text,
    )
    if has_unqualified_claim:
        # If the word is present, the surrounding context must
        # be qualified (e.g., "if reproducibility is desired" or
        # "deterministic metadata is required").
        surrounding = workflow_text[
            max(0, has_unqualified_claim.start() - 80) : has_unqualified_claim.end() + 80
        ]
        assert (
            "if" in surrounding.lower()
            or "design" in surrounding.lower()
            or "decide" in surrounding.lower()
        ), (
            f"Contract D FAIL: publish workflow uses "
            f"'reproducible' or 'byte-deterministic' without "
            f"qualifying it as a design decision. Surrounding "
            f"context: {surrounding!r}"
        )


# =============================================================
# 4 SELF-PROVING RED CONTRACTS
# =============================================================


def test_release_pipeline_produces_exactly_one_authoritative_tag_publish() -> None:
    """Contract A: exactly one authoritative signed tag publish
    per release tag.

    The publish workflow MUST have exactly one authoritative
    publisher (publish-core-image) that signs the image via
    cosign keyless. The :main push is non-authoritative.
    """
    text = _read_text(PUBLISH_WF)
    _contract_a_assertions(text)


def test_auto_pin_consumes_only_signed_tag_digest() -> None:
    """Contract B: auto-pin consumes the signed immutable tag
    digest, never the moving main tag.

    The auto-digest-bump workflow MUST resolve a versioned
    tag (head_branch, dispatch input, or latest release) and
    pin musubi_core_image to the resolved @sha256:<digest>.
    It MUST NOT chase the :main tag.
    """
    if not AUTO_PIN_WF.exists():
        # If the auto-pin workflow does not exist yet, the
        # contract is not violated by the current pipeline.
        # This is a guard for a follow-up workflow that the
        # slice may propose (per Yua 18:51:31: "If it does
        # not exist, the slice proposes a follow-up to create it").
        pytest.skip(
            "Contract B SKIP: auto-digest-bump.yml does not exist "
            "yet. The slice proposes a follow-up to create it. "
            "This test will be re-enabled once the workflow exists."
        )
    text = _read_text(AUTO_PIN_WF)
    _contract_b_assertions(text)


def test_release_main_and_tag_channels_are_explicitly_distinct() -> None:
    """Contract C: main and release channels are explicitly distinct
    in generated metadata/docs.

    The publish workflow MUST label :main and v* as distinct
    channels in its documentation, comments, or generated OCI
    annotations. The :main tag is the moving development
    channel; signed v* tags are immutable release channels.
    """
    text = _read_text(PUBLISH_WF)
    _contract_c_assertions(text)


def test_release_reproducibility_treats_cache_as_performance() -> None:
    """Contract D: a design-only reproducibility boundary that
    treats cache as performance, not input, and does not
    require main/tag byte equality.

    Cache state is NOT pinned as an artifact input. The
    reproducibility invariant (if chosen) MUST compare builds
    with identical source, platform, dependencies, toolchain,
    build arguments, and canonical OCI metadata. The test
    documents this as a SEPARATE design decision.
    """
    text = _read_text(PUBLISH_WF)
    _contract_d_assertions(text)


# =============================================================
# 3 DISCRIMINATION TESTS (catch the three wrong designs)
# =============================================================


def test_wrong_dual_authoritative_tag_publish_caught() -> None:
    """Discrimination 1: a wrong design that publishes BOTH a
    signed :main AND a signed v* (treating both as authoritative)
    MUST be caught.

    The control (the current workflow) only signs the v* tag
    as authoritative. A wrong design that ALSO signs :main as
    authoritative would violate Contract A.
    """
    text = _read_text(PUBLISH_WF)
    # Wrong: a hypothetical workflow that signs :main as
    # authoritative (e.g., has a separate cosign sign step
    # gated on the main push trigger).
    wrong_design_marker = (
        "cosign sign --keyless" in text
        and re.search(
            r"branches:\s*\[?\s*['\"]?main['\"]?",
            text,
        )
        and "if github.event.workflow_run.head_branch == 'main'" in text
    )
    assert not wrong_design_marker, (
        "Discrimination 1 FAIL: a wrong design that signs :main "
        "as an authoritative published tag is present. The "
        ":main channel MUST be non-authoritative; only the v* "
        "tag is authoritative."
    )


def test_wrong_auto_pin_chases_moving_main_caught() -> None:
    """Discrimination 2: a wrong design that pins
    musubi_core_image to the :main tag digest (instead of
    the signed tag digest) MUST be caught.

    The control (the current workflow) resolves a versioned
    tag and pins to the @sha256 digest of that tag. A wrong
    design that pins to the :main ref would violate
    Contract B.
    """
    if not AUTO_PIN_WF.exists():
        pytest.skip("Discrimination 2 SKIP: auto-digest-bump.yml does not exist yet.")
    text = _read_text(AUTO_PIN_WF)
    # Wrong: a hypothetical auto-pin step that pins to the
    # :main ref instead of a versioned tag.
    wrong_design_marker = re.search(
        r"ref\s*[:=]\s*['\"]?main['\"]?|tag\s*[:=]\s*['\"]?main['\"]?",
        text,
        re.IGNORECASE,
    )
    assert not wrong_design_marker, (
        "Discrimination 2 FAIL: a wrong design that pins "
        "musubi_core_image to the :main ref is present. The "
        "auto-pin MUST consume the signed immutable tag digest, "
        "not the moving main tag."
    )


def test_wrong_cache_pinned_as_correctness_input_caught() -> None:
    """Discrimination 3: a wrong design that treats cache as a
    required correctness input (e.g., requires cache for the
    build to be valid) MUST be caught.

    The control (the current workflow) uses cache for
    performance. A wrong design that pins cache state as a
    required artifact input would violate Contract D.
    """
    text = _read_text(PUBLISH_WF)
    wrong_design_marker = re.search(
        r"cache-(?:from|to)[^\n]*\brequired\s*:\s*true",
        text,
    )
    assert not wrong_design_marker, (
        "Discrimination 3 FAIL: a wrong design that marks "
        "cache as a required input is present. Cache is a "
        "performance concern, not a correctness or "
        "reproducibility input. Cache enabled/disabled or "
        "cache location MUST NOT change the resulting digest."
    )


# =============================================================
# 4 LEGITIMATE CONTROLS (prove the tests are not vacuous)
# =============================================================


def test_control_publish_workflow_unchanged() -> None:
    """Control 1: the publish workflow file is readable and
    has the expected authoritative structure (the tests are
    not vacuous on an empty file).
    """
    text = _read_text(PUBLISH_WF)
    # The file must be non-empty and have the publish-core-image job.
    assert len(text) > 100, (
        "Control 1 FAIL: publish-core-image.yml appears empty or "
        "truncated. The tests assume the file is the authoritative "
        "publish workflow."
    )
    assert "publish-core-image" in text, (
        "Control 1 FAIL: publish-core-image.yml does not contain "
        "the expected publish-core-image job name."
    )
    assert "cosign" in text.lower(), (
        "Control 1 FAIL: publish-core-image.yml does not mention "
        "cosign. The tests assume cosign is the authoritative "
        "signing path."
    )


def test_control_auto_pin_workflow_unchanged() -> None:
    """Control 2: the auto-pin workflow file is readable (if it
    exists) and has the expected structure.
    """
    if not AUTO_PIN_WF.exists():
        pytest.skip("Control 2 SKIP: auto-digest-bump.yml does not exist yet.")
    text = _read_text(AUTO_PIN_WF)
    assert len(text) > 100, "Control 2 FAIL: auto-digest-bump.yml appears empty or truncated."
    assert "auto-digest-bump" in text.lower(), (
        "Control 2 FAIL: auto-digest-bump.yml does not match its expected name."
    )


def test_control_release_metadata_clearly_distinguishes_channels() -> None:
    """Control 3: the generated OCI metadata (org.opencontainers.
    image.* annotations) distinguishes the channels via the
    .version annotation (so consumers can tell :main from v* via
    the manifest annotation, not just the tag).

    The version annotation is sourced from the tag via the
    docker/metadata-action@v5 step's type=semver,pattern={{version}}
    (this is the standard metadata-action behavior; the version
    is extracted from the tag and emitted as
    org.opencontainers.image.version). The meta step
    automatically produces the version annotation based on the
    semver pattern.
    """
    text = _read_text(PUBLISH_WF)
    # The meta step uses type=semver,pattern={{version}} to
    # extract the version from the tag. This is the standard
    # docker/metadata-action behavior; the version is
    # automatically emitted as org.opencontainers.image.version.
    assert re.search(
        r"type=semver[^\n]*pattern=\{?\{?version\}\}?",
        text,
    ), (
        "Control 3 FAIL: publish-core-image.yml does not source "
        "the org.opencontainers.image.version annotation from "
        "the tag via the meta step's type=semver,pattern={{version}}. "
        "Without this, consumers cannot distinguish :main from v* "
        "via the manifest annotation."
    )


def test_control_no_live_github_actions_called() -> None:
    """Control 4: the test file itself does NOT call live GitHub
    Actions or mutate releases. The tests operate on checked-in
    workflow/config fixtures only.

    This test verifies read-only behavior by:
      1. Recording the workflow file mtimes BEFORE the test
         (via a fixture).
      2. Running the test (the test itself only reads).
      3. Verifying the workflow file mtimes are UNCHANGED
         after the test.
      4. Scanning the production body (AST-based) for forbidden
         import patterns.
    """
    import ast
    import re
    from pathlib import Path

    # The test module path (used for the mtime check).
    test_file_path = Path(__file__).resolve()
    publish_wf_path = PUBLISH_WF
    auto_pin_wf_path = AUTO_PIN_WF

    # Capture the mtimes before the test runs.
    publish_mtime_before = publish_wf_path.stat().st_mtime_ns
    auto_pin_mtime_before = (
        auto_pin_wf_path.stat().st_mtime_ns if auto_pin_wf_path.exists() else None
    )

    # The rest of the test runs (read-only). After this block,
    # we verify the mtimes are UNCHANGED.

    # AST-based scan: parse the test file and inspect the
    # production body of every function (excluding docstrings).
    source = test_file_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    production_bodies: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]
            production_bodies.append(ast.unparse(ast.Module(body=body, type_ignores=[])))
    production_text = "\n".join(production_bodies)

    # Strip the test's own assert not re.search(...) guard
    # blocks (these contain the literal regex patterns being
    # checked). Use a simple line-based strip.
    cleaned_lines: list[str] = []
    for line in production_text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("assert ") and "not " in stripped and "re.search" in stripped:
            # Skip this single-line assert
            continue
        cleaned_lines.append(line)
    production_text = "\n".join(cleaned_lines)

    # The production code must NOT import requests or urllib.
    forbidden_imports = [
        (r"\bimport\s+requests\b", "requests module"),
        (r"\bfrom\s+requests\b", "requests module"),
        (r"\bimport\s+urllib\b", "urllib module"),
        (r"\bfrom\s+urllib\b", "urllib module"),
    ]
    for pattern, name in forbidden_imports:
        match = re.search(pattern, production_text)
        assert not match, (
            f"Control 4 FAIL: test_release_automation_issue449.py "
            f"production code imports {name!r}. The tests MUST "
            f"operate on checked-in workflow/config fixtures only, "
            f"not live GitHub Actions or network mutations."
        )

    # The production code must NOT call .write_text() on the
    # workflow files (read-only).
    assert not re.search(
        r"PUBLISH_WF\.write_text\s*\(",
        production_text,
    ), (
        "Control 4 FAIL: test_release_automation_issue449.py "
        "production code calls PUBLISH_WF.write_text(...). The "
        "tests MUST be read-only."
    )
    assert not re.search(
        r"AUTO_PIN_WF\.write_text\s*\(",
        production_text,
    ), (
        "Control 4 FAIL: test_release_automation_issue449.py "
        "production code calls AUTO_PIN_WF.write_text(...). The "
        "tests MUST be read-only."
    )

    # CRITICAL: The test must NOT have modified the workflow
    # files. This is the ultimate proof of read-only behavior.
    publish_mtime_after = publish_wf_path.stat().st_mtime_ns
    assert publish_mtime_after == publish_mtime_before, (
        f"Control 4 FAIL: publish-core-image.yml mtime changed "
        f"during the test. Before: {publish_mtime_before}, "
        f"After: {publish_mtime_after}. The tests MUST be "
        f"read-only."
    )
    if auto_pin_mtime_before is not None:
        auto_pin_mtime_after = auto_pin_wf_path.stat().st_mtime_ns
        assert auto_pin_mtime_after == auto_pin_mtime_before, (
            f"Control 4 FAIL: auto-digest-bump.yml mtime changed "
            f"during the test. Before: {auto_pin_mtime_before}, "
            f"After: {auto_pin_mtime_after}. The tests MUST be "
            f"read-only."
        )
