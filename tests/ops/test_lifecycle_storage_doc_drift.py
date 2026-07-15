"""Bounded discriminator (C6b D0): the named current-state operational docs must NOT
name the retired lifecycle storage FILE.

Scans ONLY this fixed doc set (Yua ruling, 2026-07-14) so the retired absolute path or
basename ``lifecycle-work.sqlite`` cannot return unnoticed after the §E FILE->DIR
reconciliation. Deliberately OUT of scope: the C6b source-cut plan passages that
describe the retired source, test fixtures, and the ``lifecycle-worker`` service name.
This test does not scan the migration DESIGN or fixture strings — only the enumerated
current-state docs.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

RETIRED_FILE_TOKEN = "lifecycle-work.sqlite"
CANONICAL_DIR_DB = "lifecycle/work.sqlite"

# The exact named current-state doc set (§E config-drift closure scope).
CURRENT_STATE_DOCS = (
    "docs/Musubi/08-deployment/compose-stack.md",
    "docs/Musubi/08-deployment/host-profile.md",
    "docs/Musubi/09-operations/runbooks.md",
    "docs/Musubi/09-operations/index.md",
    "docs/Musubi/09-operations/asset-matrix.md",
    "docs/Musubi/09-operations/backup-restore.md",
    "docs/Musubi/10-security/data-handling.md",
    "docs/Musubi/11-migration/phase-2-hybrid-search.md",
    "docs/Musubi/11-migration/re-embedding.md",
    "docs/Musubi/11-migration/phase-6-lifecycle.md",
)


def _names_retired_file(rel: str) -> bool:
    return RETIRED_FILE_TOKEN in (ROOT / rel).read_text()


def test_named_current_state_docs_reject_the_retired_lifecycle_file() -> None:
    for rel in CURRENT_STATE_DOCS:
        assert (ROOT / rel).exists(), f"named current-state doc is missing: {rel}"
    offenders = [rel for rel in CURRENT_STATE_DOCS if _names_retired_file(rel)]
    assert not offenders, (
        f"retired lifecycle storage token {RETIRED_FILE_TOKEN!r} returned in current-state "
        f"docs {offenders}; use the canonical DIR DB {CANONICAL_DIR_DB!r}"
    )


def test_scan_discriminates_retired_vs_canonical() -> None:
    """GREEN mechanism proof: the token check CATCHES the retired FILE and PASSES the
    canonical DIR DB, so the guard above is a real discriminator, not a vacuous pass."""
    retired_line = f"LIFECYCLE_SQLITE_PATH=/var/lib/musubi/{RETIRED_FILE_TOKEN}"
    canonical_line = f"LIFECYCLE_SQLITE_PATH=/var/lib/musubi/{CANONICAL_DIR_DB}"
    assert RETIRED_FILE_TOKEN in retired_line  # a regressed doc WOULD be caught
    assert RETIRED_FILE_TOKEN not in canonical_line  # the aligned form is NOT a false positive
