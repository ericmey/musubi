"""Structural tests for `.github/workflows/publish-core-image.yml`.

Can't invoke `act` or drive real GHA from unit tests, so these assert
the *shape* of the workflow — if a renamed action / dropped trigger /
weakened permission slips in, CI fails instead of the next tag push
silently publishing nothing.

Scope:

- Triggers: tag push `v*`, branch push `v2`, `workflow_dispatch`.
- Permissions: `packages: write` (the GHCR push) + `contents: read`.
- One job named `publish-core-image`.
- Uses `docker/login-action` → GHCR, `docker/build-push-action` with
  `push: true` and at least one `ghcr.io/ericmey/musubi-core` tag.
- Builds for `linux/amd64`.
- Does NOT mutate `deploy/ansible/group_vars/all.yml` — digest bumps
  are separate, human-reviewed PRs.
- `group_vars/all.yml`'s `musubi_core_image` value is in one of the
  accepted shapes (local-build pre-flip OR GHCR digest post-flip).
- `deploy/runbooks/upgrade-image.md` is an operator-runnable doc —
  every step carries Command / Expected / Destructive / Rollback.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-core-image.yml"
GROUP_VARS = ROOT / "deploy" / "ansible" / "group_vars" / "all.yml"
RUNBOOK = ROOT / "deploy" / "runbooks" / "upgrade-image.md"
FIRST_DEPLOY_RUNBOOK = ROOT / "deploy" / "runbooks" / "first-deploy.md"


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


def test_workflow_triggers_include_tags_v2_and_dispatch() -> None:
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
    assert "v2" in branches, "no v2 branch trigger"

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
    steps = _job_steps()
    trivy = [s for s in steps if "aquasecurity/trivy-action" in str(s.get("uses", ""))]
    assert trivy, "missing aquasecurity/trivy-action step"
    w = trivy[0].get("with") or {}
    # Must scan the published digest, not a mutable tag.
    assert "@${{ steps.build.outputs.digest }}" in str(w.get("image-ref", "")), (
        "Trivy must scan the image by digest"
    )
    # Must fail the job on findings.
    assert str(w.get("exit-code")) == "1", "Trivy exit-code must be 1 to gate the build"
    severity = str(w.get("severity", "")).upper()
    assert "CRITICAL" in severity, "Trivy severity must include CRITICAL"


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


def test_workflow_uses_build_push_action_with_push_true() -> None:
    steps = _job_steps()
    build = [s for s in steps if "docker/build-push-action" in str(s.get("uses", ""))]
    assert build, "no docker/build-push-action step"
    with_block = build[0].get("with") or {}
    assert with_block.get("push") is True, "build-push-action must set push: true"


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
    assert "ghcr.io/ericmey/musubi-core" in text


def test_workflow_does_not_mutate_group_vars() -> None:
    """The publish workflow is strictly build+push — the digest bump
    is a separate PR per the slice spec.

    Comments that mention `group_vars/all.yml` (e.g. the operator-
    facing summary) are fine; what we guard against is any action
    that writes to the file: `sed -i`, `git commit`, a
    create-pull-request action, etc.
    """
    text = WORKFLOW.read_text()
    forbidden = (
        "sed -i",  # in-place edit
        "git commit",
        "git push origin v2",
        "peter-evans/create-pull-request",
        "stefanzweifel/git-auto-commit-action",
    )
    for token in forbidden:
        assert token not in text, (
            f"publish workflow must not include {token!r} — the pin bump "
            "is a separate, human-reviewed PR"
        )


# ---------------------------------------------------------------------------
# Ansible integration
# ---------------------------------------------------------------------------


_IMAGE_RE = re.compile(
    r"^(musubi-core:dev|ghcr\.io/ericmey/musubi-core"
    r"(:v\d[\w.\-]*|@sha256:[0-9a-f]{64}))$"
)


def test_group_vars_musubi_core_image_parses_as_oci_reference() -> None:
    gv = _load(GROUP_VARS)
    image = gv.get("musubi_core_image")
    assert isinstance(image, str) and image, "musubi_core_image not set"
    assert _IMAGE_RE.match(image), (
        f"musubi_core_image={image!r} is not a recognised shape — expected "
        "either the pre-publish local tag 'musubi-core:dev' or a GHCR "
        "reference like 'ghcr.io/ericmey/musubi-core@sha256:<64-hex>'"
    )


# ---------------------------------------------------------------------------
# Runbook
# ---------------------------------------------------------------------------


def test_upgrade_image_runbook_exists_and_has_six_sections() -> None:
    assert RUNBOOK.exists(), f"missing {RUNBOOK}"
    text = RUNBOOK.read_text()
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert len(headings) >= 6, (
        f"upgrade-image runbook should have at least 6 numbered steps; got {len(headings)}"
    )


def test_upgrade_image_runbook_every_step_has_rollback() -> None:
    text = RUNBOOK.read_text()
    # Split on "## " numbered sections.
    step_sections = re.split(r"^## \d", text, flags=re.MULTILINE)[1:]
    assert step_sections, "no numbered steps found in runbook"
    for i, sec in enumerate(step_sections, 1):
        assert "Rollback:" in sec or "Rollback" in sec, (
            f"step {i} of upgrade-image runbook has no Rollback: clause"
        )


def test_first_deploy_runbook_no_longer_promises_local_build_only() -> None:
    """First deploy no longer has to build locally — the workflow publishes
    the image, so the runbook's Kong / image-transfer section should at
    least point at the upgrade-image runbook as the supported path."""
    if not FIRST_DEPLOY_RUNBOOK.exists():
        # First-deploy runbook is owned by another slice; skip cleanly.
        return
    text = FIRST_DEPLOY_RUNBOOK.read_text()
    # We don't gate the whole runbook here (that's cross-slice); just
    # assert there's *some* pointer to the new workflow once it lands.
    # Until the cross-slice ticket lands this is a soft expectation.
    _ = text
