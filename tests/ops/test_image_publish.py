"""Structural tests for `.github/workflows/publish-core-image.yml`.

Can't invoke `act` or drive real GHA from unit tests, so these assert
the *shape* of the workflow — if a renamed action / dropped trigger /
weakened permission slips in, CI fails instead of the next tag push
silently publishing nothing.

Scope:

- Triggers: tag push `v*`, branch push `main`, `workflow_dispatch`.
- Permissions: `packages: write` (the GHCR push) + `contents: write`
  (the release asset).
- One job named `publish-core-image`.
- Uses `docker/login-action` → GHCR, builds one local scan candidate,
  and publishes that exact image only after the CRITICAL gate.
- Builds for `linux/amd64`.
- Does NOT mutate `docker-compose.yml` — digest bumps
  are separate, human-reviewed PRs.
- The public Compose Core image is pinned to a GHCR digest.
- The public operator guide verifies the image and documents rollback.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-core-image.yml"
PUBLIC_COMPOSE = ROOT / "docker-compose.yml"
OPERATE_GUIDE = ROOT / "docs" / "guide" / "operate.md"
PUSH_DIGEST_EXTRACTOR = "grep -oE 'sha256:[0-9a-f]{64}' | tail -1"


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def _workflow() -> dict[str, Any]:
    return _load(WORKFLOW)  # type: ignore[no-any-return]


def _job() -> dict[str, Any]:
    jobs = _workflow().get("jobs") or {}
    assert "publish-core-image" in jobs, "missing publish-core-image job"
    return jobs["publish-core-image"]  # type: ignore[no-any-return]


def _job_steps() -> list[dict[str, Any]]:
    return list(_job().get("steps") or [])


# ---------------------------------------------------------------------------
# Workflow structure
# ---------------------------------------------------------------------------


def test_workflow_file_parses() -> None:
    assert WORKFLOW.exists(), f"missing {WORKFLOW}"
    wf = _workflow()
    assert isinstance(wf, dict)
    assert "jobs" in wf


def test_workflow_has_publish_core_image_job() -> None:
    job = _job()
    assert job.get("runs-on") == "ubuntu-latest"


def test_workflow_triggers_include_tags_main_and_dispatch() -> None:
    # YAML quirk: `on:` parses to the boolean `True` under safe_load
    # because `on` is an alias for true in YAML 1.1. Look it up
    # defensively so we don't trip on that.
    wf = _workflow()
    on_block: Any = wf.get("on")
    if on_block is None:
        on_block = wf.get(True)  # type: ignore[call-overload]
    assert on_block, "no triggers section"

    push = on_block.get("push") or {}
    tags = push.get("tags") or []
    assert any(pat.startswith("v") for pat in tags), "no v* tag trigger"

    branches = push.get("branches") or []
    assert "main" in branches, "no main branch trigger"

    assert "workflow_dispatch" in on_block, "no workflow_dispatch trigger"


def test_workflow_requests_packages_write_permission() -> None:
    perms = _job().get("permissions") or {}
    assert perms.get("packages") == "write", "missing packages:write permission"
    assert perms.get("contents") in ("read", "write"), "missing contents permission"
    # id-token:write is required for cosign keyless signing via GitHub
    # OIDC. Without it, the sign step silently tries to open a browser
    # for oauth — which of course fails in CI.
    assert perms.get("id-token") == "write", (
        "missing id-token:write — cosign keyless signing needs GitHub OIDC"
    )
    assert perms.get("attestations") == "write", (
        "missing attestations:write — build provenance cannot be published"
    )


# ---------------------------------------------------------------------------
# Supply-chain hardening (Tier 1)
# ---------------------------------------------------------------------------


def test_workflow_signs_image_via_cosign_keyless() -> None:
    """Every published digest MUST be cosign-signed so pullers can later
    verify against this repo's workflow identity."""
    steps = _job_steps()
    installer = [s for s in steps if "sigstore/cosign-installer" in str(s.get("uses", ""))]
    assert installer, "missing sigstore/cosign-installer step"

    # The sign step either uses an action or `run:`s cosign directly.
    sign_step = None
    for s in steps:
        run = str(s.get("run", ""))
        if "cosign sign" in run and "--yes" in run:
            sign_step = s
            break
    assert sign_step, "no cosign sign step found"
    # Must sign by digest, not by tag (tags are mutable).
    assert "@${{ steps.build.outputs.digest }}" in str(sign_step.get("run", "")), (
        "cosign sign must target the image by digest, not tag"
    )


def test_workflow_generates_sbom() -> None:
    steps = _job_steps()
    sbom = [s for s in steps if "anchore/sbom-action" in str(s.get("uses", ""))]
    assert sbom, "missing anchore/sbom-action step (SBOM generation)"
    with_block = sbom[0].get("with") or {}
    assert "cyclonedx" in str(with_block.get("format", "")).lower(), (
        "SBOM format should be CycloneDX (the cross-tool standard)"
    )


def test_workflow_attests_published_digest_provenance() -> None:
    steps = _job_steps()
    attest = [s for s in steps if "actions/attest-build-provenance" in str(s.get("uses", ""))]
    assert len(attest) == 1, "published image needs one provenance attestation"
    with_block = attest[0].get("with") or {}
    assert with_block.get("subject-digest") == "${{ steps.build.outputs.digest }}"
    assert with_block.get("push-to-registry") is True


def test_workflow_attaches_sbom_as_cosign_attestation() -> None:
    steps = _job_steps()
    for s in steps:
        run = str(s.get("run", ""))
        if "cosign attest" in run and "cyclonedx" in run:
            # Also must target by digest.
            assert "@${{ steps.build.outputs.digest }}" in run
            return
    raise AssertionError("no cosign attest step for the CycloneDX SBOM")


def test_workflow_trivy_scans_for_critical_cves_and_fails_on_finding() -> None:
    """At least one Trivy step must fail the build on CRITICAL findings.
    Workflow may have a table-format pre-scan for log visibility (exit-code
    0) AND the SARIF gate (exit-code 1); only the gate matters here."""
    steps = _job_steps()
    trivy_steps = [s for s in steps if "aquasecurity/trivy-action" in str(s.get("uses", ""))]
    assert trivy_steps, "missing aquasecurity/trivy-action step"

    gate = None
    for s in trivy_steps:
        w = s.get("with") or {}
        if str(w.get("exit-code")) == "1":
            gate = w
            break
    assert gate is not None, (
        "no Trivy step with exit-code: 1 — at least one step must gate the build"
    )
    assert gate.get("image-ref") == "musubi-core:scan-${{ github.sha }}", (
        "Trivy gate must scan the local pre-publish candidate"
    )
    severity = str(gate.get("severity", "")).upper()
    assert "CRITICAL" in severity, "Trivy gate severity must include CRITICAL"


def test_critical_gate_runs_before_any_registry_push() -> None:
    """A vulnerable image must never acquire a GHCR tag before refusal."""
    steps = _job_steps()
    login_index = next(
        i for i, step in enumerate(steps) if "docker/login-action" in str(step.get("uses", ""))
    )
    gate_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Trivy vulnerability scan (SARIF — CRITICAL gate)"
    )
    pre_gate_names = [step.get("name") for step in steps[login_index + 1 : gate_index]]
    assert pre_gate_names == [
        "Derive image tags + labels",
        "Build local scan candidate",
        "Trivy vulnerability scan (table — always visible in logs)",
    ], "unexpected step can write to the registry before the CRITICAL gate"

    candidate = next(step for step in steps if step.get("name") == "Build local scan candidate")
    with_block = candidate.get("with") or {}
    assert with_block.get("load") is True
    assert with_block.get("push") is False


def test_publisher_retags_the_exact_scanned_image_without_rebuilding() -> None:
    steps = _job_steps()
    builds = [s for s in steps if "docker/build-push-action" in str(s.get("uses", ""))]
    assert len(builds) == 1, "workflow must build exactly once"

    publisher = next(step for step in steps if step.get("name") == "Publish scanned image")
    run = str(publisher.get("run", ""))
    assert 'docker tag "$SCAN_IMAGE" "$tag"' in run
    assert 'docker push "$tag"' in run
    assert "docker build" not in run


def test_publisher_uses_each_push_receipt_as_the_digest_source() -> None:
    publisher = next(step for step in _job_steps() if step.get("name") == "Publish scanned image")
    run = str(publisher.get("run", ""))
    assert 'push_output="$(docker push "$tag" 2>&1)"' in run
    assert PUSH_DIGEST_EXTRACTOR.replace(" |", ' <<< "$push_output" |') in run
    assert '[[ "$tag_digest" == sha256:* ]]' in run
    assert '[[ "$tag_digest" == "$published_digest" ]]' in run
    assert 'echo "digest=$published_digest" >> "$GITHUB_OUTPUT"' in run
    assert "RepoDigests" not in run


def test_push_digest_extractor_accepts_real_docker_output() -> None:
    digest = "sha256:3e2e847869a190a1819a2b46338b50c6742024b36457aafc1d956ce764194ad8"
    transcript = "\n".join(
        [
            "The push refers to repository [127.0.0.1:5999/shiori-probe]",
            "cae91b5c4165: Pushed",
            f"main: digest: {digest} size: 855",
        ]
    )
    result = subprocess.run(
        ["bash", "-o", "pipefail", "-c", PUSH_DIGEST_EXTRACTOR],
        input=transcript,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == digest


def test_workflow_grants_security_events_write_for_sarif_upload() -> None:
    """`upload-sarif@v3` requires security-events:write on the job token."""
    perms = _job().get("permissions") or {}
    assert perms.get("security-events") == "write", (
        "missing security-events:write — upload-sarif fails without it"
    )


def test_workflow_uploads_trivy_sarif_to_code_scanning() -> None:
    """Findings must reach the Security tab even when the scan failed,
    otherwise operators can't see what broke."""
    steps = _job_steps()
    upload = [s for s in steps if "github/codeql-action/upload-sarif" in str(s.get("uses", ""))]
    assert upload, "missing upload-sarif step"
    # Must run even on prior-step failure.
    assert str(upload[0].get("if", "")).strip() == "always()"


def test_workflow_logs_into_ghcr() -> None:
    steps = _job_steps()
    login = [s for s in steps if "docker/login-action" in str(s.get("uses", ""))]
    assert login, "no docker/login-action step"
    registry = (login[0].get("with") or {}).get("registry")
    assert registry == "ghcr.io", f"login registry is {registry!r}, expected ghcr.io"


def test_workflow_uses_build_push_action_for_local_candidate() -> None:
    steps = _job_steps()
    build = [s for s in steps if "docker/build-push-action" in str(s.get("uses", ""))]
    assert build, "no docker/build-push-action step"
    with_block = build[0].get("with") or {}
    assert with_block.get("load") is True, "scan candidate must load into Docker"
    assert with_block.get("push") is False, "build must not publish before Trivy"


def test_workflow_builds_for_linux_amd64() -> None:
    steps = _job_steps()
    build = [s for s in steps if "docker/build-push-action" in str(s.get("uses", ""))]
    with_block = build[0].get("with") or {}
    platforms = str(with_block.get("platforms") or "")
    assert "linux/amd64" in platforms


def test_workflow_tags_include_ghcr_namespace() -> None:
    # docker/metadata-action emits the tags; grep for the canonical
    # namespace in the workflow source as a cheap integration test.
    text = WORKFLOW.read_text()
    assert "IMAGE: ghcr.io/${{ github.repository_owner }}/musubi-core" in text
    assert "images: ${{ env.IMAGE }}" in text


def test_workflow_does_not_mutate_compose() -> None:
    """The publish workflow is strictly build+push — the digest bump
    is a separate PR per the slice spec.

    Comments that mention `docker-compose.yml` (e.g. the operator-
    facing summary) are fine; what we guard against is any action
    that writes to the file: `sed -i`, `git commit`, a
    create-pull-request action, etc.
    """
    text = WORKFLOW.read_text()
    forbidden = (
        "sed -i",  # in-place edit
        "git commit",
        "git push origin main",
        "peter-evans/create-pull-request",
        "stefanzweifel/git-auto-commit-action",
    )
    for token in forbidden:
        assert token not in text, (
            f"publish workflow must not include {token!r} — the pin bump "
            "is a separate, human-reviewed PR"
        )


def test_release_summary_points_at_public_compose() -> None:
    workflow = WORKFLOW.read_text()
    assert "docker-compose.yml and quickstart/docker-compose.yml" in workflow
    assert "deploy/ansible/group_vars/all.yml" not in workflow


# ---------------------------------------------------------------------------
# Public Compose integration
# ---------------------------------------------------------------------------


_IMAGE_RE = re.compile(r"^ghcr\.io/sourceblender/musubi-core@sha256:[0-9a-f]{64}$")


def test_public_compose_core_image_is_pinned_to_digest() -> None:
    compose = _load(PUBLIC_COMPOSE)
    image = compose.get("x-core-image", "").strip()
    assert isinstance(image, str) and image, "public Compose Core image not set"
    assert _IMAGE_RE.match(image), f"x-core-image={image!r} must be a sourceblender GHCR digest pin"


# ---------------------------------------------------------------------------
# Public operator guide
# ---------------------------------------------------------------------------


def test_public_operate_guide_verifies_and_rolls_back_the_compose_pin() -> None:
    text = OPERATE_GUIDE.read_text()
    assert "x-core-image" in text
    assert "cosign verify" in text
    assert "docker compose up -d --wait" in text
    assert "previous pin" in text
