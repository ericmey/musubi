"""The image namespace and signer identity survive `ericmey/musubi` → `sourceblender/musubi`.

After a GitHub transfer, the publish workflow runs as the new owner: its
token cannot write `ghcr.io/ericmey/*`, and Sigstore records the new repo in
the signing certificate. Every consumer must therefore accept both owners
through the transition, and the publisher must derive its namespace from the
repo that runs it rather than naming one.

These tests exercise the extracted patterns against concrete inputs instead of
checking that a guard is merely present.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.release.test_release_automation_issue449 import PROJECT_RELEASE_GRAMMAR_BASH

ROOT = Path(__file__).resolve().parents[2]
PUBLISH = ROOT / ".github" / "workflows" / "publish-core-image.yml"
DIGEST_BUMP = ROOT / ".github" / "workflows" / "auto-digest-bump.yml"
UPDATE_PLAYBOOK = ROOT / "deploy" / "ansible" / "update.yml"
RUNBOOK = ROOT / "deploy" / "runbooks" / "upgrade-image.md"
# Public verify instructions trust any workflow in the repo; narrowing that is a
# separate decision, so here only the owner set and the anchor are asserted.
# The user-facing verify command lives in the user guide; README links to it.
GUIDE_INSTALL = ROOT / "docs" / "guide" / "install.md"
PUBLIC_DOCS = (GUIDE_INSTALL, ROOT / "SECURITY.md")
PUBLIC_IDENTITY = r"^https://github\.com/(ericmey|sourceblender)/musubi/.*"

CANONICAL_IDENTITY = (
    r"^https://github\.com/(ericmey|sourceblender)/musubi/"
    r"\.github/workflows/publish-core-image\.yml@refs/tags/"
    # The tag suffix is the project release grammar (Issue #449), the same one
    # auto-digest-bump uses to decide which tags get a pin PR. Two grammars would
    # let a pin PR open for a tag its own verifier then rejects.
    + PROJECT_RELEASE_GRAMMAR_BASH.removeprefix("^")
)
DIGEST = "sha256:" + "a" * 64
WORKFLOW = ".github/workflows/publish-core-image.yml"


def _identities(text: str) -> list[str]:
    """Every regexp passed to --certificate-identity-regexp, in either CLI or argv form."""
    flag = re.findall(r"--certificate-identity-regexp '([^']+)'", text)
    argv = re.findall(r"- --certificate-identity-regexp\n\s*- '([^']+)'", text)
    return flag + argv


def test_every_verifier_uses_the_one_canonical_identity() -> None:
    sources = (UPDATE_PLAYBOOK, DIGEST_BUMP, RUNBOOK)
    for path in sources:
        found = _identities(path.read_text())
        assert found, f"{path.relative_to(ROOT)}: no --certificate-identity-regexp found"
        assert set(found) == {CANONICAL_IDENTITY}, (path.relative_to(ROOT), found)


def test_readme_reaches_the_public_verify_instructions() -> None:
    """Moving the verify command out of README must not orphan it."""
    readme = (ROOT / "README.md").read_text()
    guide_index = (ROOT / "docs" / "guide" / "README.md").read_text()
    assert "(docs/guide/README.md)" in readme
    assert "(install.md)" in guide_index
    assert GUIDE_INSTALL.exists()


def test_public_verify_docs_accept_both_owners_anchored() -> None:
    for path in PUBLIC_DOCS:
        assert set(_identities(path.read_text())) == {PUBLIC_IDENTITY}, path.name
    pattern = re.compile(PUBLIC_IDENTITY)
    for owner in ("ericmey", "sourceblender"):
        assert pattern.search(f"https://github.com/{owner}/musubi/{WORKFLOW}@refs/tags/v1.0.0")
    assert not pattern.search(
        f"https://github.com/evil/x/{WORKFLOW}@refs/heads/https://github.com/ericmey/musubi/"
    )


def test_identity_accepts_both_owners_release_tags() -> None:
    # cosign matches with Go's regexp.MatchString (unanchored), so re.search is the model.
    pattern = re.compile(CANONICAL_IDENTITY)
    for owner in ("ericmey", "sourceblender"):
        for tag in ("v1.27.0", "v1.27.0-rc.1", "v2.0.0-alpha"):
            assert pattern.search(f"https://github.com/{owner}/musubi/{WORKFLOW}@refs/tags/{tag}")


def test_identity_rejects_lookalikes_and_non_release_refs() -> None:
    pattern = re.compile(CANONICAL_IDENTITY)
    rejected = (
        f"https://github.com/evil/musubi/{WORKFLOW}@refs/tags/v1.0.0",
        f"https://github.com/ericmey/musubi-fork/{WORKFLOW}@refs/tags/v1.0.0",
        f"https://github.com/sourceblender-evil/musubi/{WORKFLOW}@refs/tags/v1.0.0",
        f"https://githubXcom/ericmey/musubi/{WORKFLOW}@refs/tags/v1.0.0",
        "https://github.com/ericmey/musubi/.github/workflows/other.yml@refs/tags/v1.0.0",
        f"https://github.com/ericmey/musubi/{WORKFLOW}@refs/heads/main",
        # Tag suffixes outside the release grammar (Tama, cosign probe 2026-09-29).
        f"https://github.com/ericmey/musubi/{WORKFLOW}@refs/tags/v1.27.0/forged",
        f"https://github.com/ericmey/musubi/{WORKFLOW}@refs/tags/v1.27.0.evil",
        f"https://github.com/ericmey/musubi/{WORKFLOW}@refs/tags/v1.27.0+build.5",
        f"https://github.com/ericmey/musubi/{WORKFLOW}@refs/tags/v1.27.0-rc.1/forged",
        f"https://github.com/ericmey/musubi/{WORKFLOW}@refs/tags/v01.2.3",
        f"https://github.com/ericmey/musubi/{WORKFLOW}@refs/tags/v1.27",
        # Unanchored, the old pattern accepted the trusted identity embedded anywhere.
        f"https://github.com/evil/x/.github/workflows/a.yml@refs/heads/"
        f"https://github.com/ericmey/musubi/{WORKFLOW}@refs/tags/v1",
    )
    for san in rejected:
        assert not pattern.search(san), san


def test_update_playbook_image_guard_accepts_both_owners_only() -> None:
    text = UPDATE_PLAYBOOK.read_text()
    guards = re.findall(r"musubi_core_image is match\('([^']+)'\)", text)
    assert len(guards) == 1, guards
    # Ansible's `match` test is re.match.
    guard = re.compile(guards[0])
    for owner in ("ericmey", "sourceblender"):
        assert guard.match(f"ghcr.io/{owner}/musubi-core@{DIGEST}")
    for image in (
        f"ghcr.io/evil/musubi-core@{DIGEST}",
        "ghcr.io/sourceblender/musubi-core:v1.27.0",
        f"ghcr.io/sourceblender-evil/musubi-core@{DIGEST}",
        f"ghcrXio/ericmey/musubi-core@{DIGEST}",
    ):
        assert not guard.match(image), image


def test_publisher_derives_namespace_from_repository_owner() -> None:
    text = PUBLISH.read_text()
    assert "IMAGE: ghcr.io/${{ github.repository_owner }}/musubi-core" in text
    assert "org.opencontainers.image.source=https://github.com/${{ github.repository }}" in text
    assert not re.search(r"ghcr\.io/(ericmey|sourceblender)/", text)


def test_digest_bump_resolves_and_pins_in_the_running_owners_namespace() -> None:
    text = DIGEST_BUMP.read_text()
    assert "IMAGE_REPO: ${{ github.repository_owner }}/musubi-core" in text
    assert not re.search(r"ghcr\.io/(ericmey|sourceblender)/", text)
    assert not re.search(r'IMAGE="(ericmey|sourceblender)/', text)
