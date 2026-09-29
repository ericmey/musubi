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

ROOT = Path(__file__).resolve().parents[2]
PUBLISH = ROOT / ".github" / "workflows" / "publish-core-image.yml"
DIGEST_BUMP = ROOT / ".github" / "workflows" / "auto-digest-bump.yml"
UPDATE_PLAYBOOK = ROOT / "deploy" / "ansible" / "update.yml"
IDENTITY_DOCS = (
    ROOT / "deploy" / "runbooks" / "upgrade-image.md",
    ROOT / "README.md",
    ROOT / "SECURITY.md",
)

CANONICAL_IDENTITY = (
    r"^https://github\.com/(ericmey|sourceblender)/musubi/"
    r"\.github/workflows/publish-core-image\.yml@refs/tags/v.*"
)
DIGEST = "sha256:" + "a" * 64
WORKFLOW = ".github/workflows/publish-core-image.yml"


def _identities(text: str) -> list[str]:
    """Every regexp passed to --certificate-identity-regexp, in either CLI or argv form."""
    flag = re.findall(r"--certificate-identity-regexp '([^']+)'", text)
    argv = re.findall(r"- --certificate-identity-regexp\n\s*- '([^']+)'", text)
    return flag + argv


def test_every_verifier_uses_the_one_canonical_identity() -> None:
    sources = (UPDATE_PLAYBOOK, DIGEST_BUMP, *IDENTITY_DOCS)
    for path in sources:
        found = _identities(path.read_text())
        assert found, f"{path.relative_to(ROOT)}: no --certificate-identity-regexp found"
        assert set(found) == {CANONICAL_IDENTITY}, (path.relative_to(ROOT), found)


def test_identity_accepts_both_owners_release_tags() -> None:
    # cosign matches with Go's regexp.MatchString (unanchored), so re.search is the model.
    pattern = re.compile(CANONICAL_IDENTITY)
    for owner in ("ericmey", "sourceblender"):
        assert pattern.search(f"https://github.com/{owner}/musubi/{WORKFLOW}@refs/tags/v1.27.0")


def test_identity_rejects_lookalikes_and_non_release_refs() -> None:
    pattern = re.compile(CANONICAL_IDENTITY)
    rejected = (
        f"https://github.com/evil/musubi/{WORKFLOW}@refs/tags/v1.0.0",
        f"https://github.com/ericmey/musubi-fork/{WORKFLOW}@refs/tags/v1.0.0",
        f"https://github.com/sourceblender-evil/musubi/{WORKFLOW}@refs/tags/v1.0.0",
        f"https://githubXcom/ericmey/musubi/{WORKFLOW}@refs/tags/v1.0.0",
        f"https://github.com/ericmey/musubi/.github/workflows/other.yml@refs/tags/v1.0.0",
        f"https://github.com/ericmey/musubi/{WORKFLOW}@refs/heads/main",
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
        f"ghcr.io/sourceblender/musubi-core:v1.27.0",
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
