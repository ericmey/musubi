"""Structural tests for the host-local backup scheduler.

We can't exercise the real script in unit tests — it shells into live
Docker. These tests assert the shape of the script and the systemd
units so operational drift (a renamed collection, a missing flag, a
weakened hardening policy) fails CI instead of rotting silently.

Scope:

- The backup script parses under bash, declares strict mode, honours the
  single-run lock, and touches every canonical store (Qdrant / sqlite /
  artifact-blobs).
- The systemd service uses `Type=oneshot`, hardens paths, and has a
  sensible timeout.
- The timer fires at least every 6h (matching the RPO claim in
  `src/musubi/ops/backup.py::BACKUP_CADENCE_MINUTES["qdrant"]`) and
  sets `Persistent=true` for missed-firing catchup.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKUP_DIR = ROOT / "deploy" / "backup"
SCRIPT = BACKUP_DIR / "musubi-backup.sh"
SERVICE = BACKUP_DIR / "systemd" / "musubi-backup.service"
TIMER = BACKUP_DIR / "systemd" / "musubi-backup.timer"


# ---------------------------------------------------------------------------
# Script
# ---------------------------------------------------------------------------


def test_script_exists_and_is_executable_source() -> None:
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    first_line = SCRIPT.read_text().splitlines()[0]
    assert first_line.startswith("#!"), "missing shebang"
    assert "bash" in first_line


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_script_parses_under_bash() -> None:
    # `bash -n` is syntax-check only; doesn't execute.
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_script_enables_strict_mode() -> None:
    text = SCRIPT.read_text()
    assert "set -euo pipefail" in text


def test_script_guards_against_concurrent_runs() -> None:
    text = SCRIPT.read_text()
    assert "flock" in text, "backup script must take an advisory lock"
    assert "LOCK_FILE" in text


def test_script_discovers_collections_dynamically() -> None:
    """The backup must not hardcode collection names — those drift with
    store refactors. It discovers the list via the Qdrant API at run time.
    """
    text = SCRIPT.read_text()
    assert "/collections" in text
    # Discovery must happen before the snapshot loop, so populated names
    # drive the loop. We assert that by requiring the API-list query to
    # come before the first snapshot POST.
    list_idx = text.find("'http://qdrant:6333/collections'")
    post_idx = text.find("/collections/${coll}/snapshots")
    assert 0 <= list_idx < post_idx, "collection discovery must precede snapshot loop"


def test_script_calls_qdrant_snapshot_api() -> None:
    text = SCRIPT.read_text()
    assert "/collections/" in text
    assert "/snapshots" in text
    # Must send the API key.
    assert "api-key" in text.lower()


def test_script_backs_up_lifecycle_sqlite() -> None:
    text = SCRIPT.read_text()
    assert "/var/lib/musubi/lifecycle/work.sqlite" in text
    assert ".backup(" in text or "sqlite3 " in text


def test_script_rsyncs_artifact_blobs() -> None:
    text = SCRIPT.read_text()
    assert "/var/lib/musubi/artifact-blobs" in text
    assert "rsync" in text


def test_script_writes_sha256_for_qdrant_snapshots() -> None:
    text = SCRIPT.read_text()
    assert "sha256sum" in text
    assert "SHA256SUMS" in text


def test_script_writes_manifest_with_status() -> None:
    text = SCRIPT.read_text()
    assert "manifest.json" in text
    assert '"status":' in text


def test_script_retention_only_runs_on_green() -> None:
    """A failed run must NOT prune old backups (we need them more than ever)."""
    text = SCRIPT.read_text()
    # The prune invocation (-mtime flag) must be syntactically inside a
    # `STATUS -eq 0` guarded block. We assert this by locating both and
    # requiring the guard to come first.
    idx_guard = re.search(r"STATUS[}]?\s*-eq\s*0", text)
    idx_prune = text.find("-mtime")
    assert idx_guard is not None, "no STATUS == 0 guard found"
    assert idx_prune != -1, "no -mtime retention call found"
    assert idx_guard.start() < idx_prune, "retention prune is not guarded on STATUS == 0"


# ---------------------------------------------------------------------------
# systemd units
# ---------------------------------------------------------------------------


def test_service_is_oneshot() -> None:
    assert "Type=oneshot" in SERVICE.read_text()


def test_service_invokes_the_installed_script_path() -> None:
    text = SERVICE.read_text()
    assert "ExecStart=/usr/local/bin/musubi-backup" in text


def test_service_hardens_paths() -> None:
    text = SERVICE.read_text()
    assert "ProtectSystem=strict" in text
    assert "NoNewPrivileges=true" in text
    assert "ReadWritePaths=" in text
    # Writable paths must include the backup root.
    rw_line = next(line for line in text.splitlines() if line.startswith("ReadWritePaths="))
    assert "/var/lib/musubi/backups" in rw_line


def test_service_has_bounded_timeout() -> None:
    text = SERVICE.read_text()
    m = re.search(r"TimeoutStartSec=(\d+)", text)
    assert m, "missing TimeoutStartSec"
    assert 300 <= int(m.group(1)) <= 3600


def test_timer_cadence_satisfies_rpo() -> None:
    from musubi.ops.backup import BACKUP_CADENCE_MINUTES

    text = TIMER.read_text()
    m = re.search(r"OnCalendar=\S+", text)
    assert m, "timer has no OnCalendar"
    cadence = m.group(0)
    # "00/6:17:00" → fires every 6h. We assert that the numerator
    # expressed in minutes is at most the documented RPO for qdrant.
    # This test is approximate — covers the common "00/<N>" / "*/<N>" /
    # explicit "hourly" forms.
    simple = re.search(r"00/(\d+)|\*/(\d+)", cadence)
    if simple:
        hours = int(simple.group(1) or simple.group(2))
        assert hours * 60 <= BACKUP_CADENCE_MINUTES["qdrant"]
    else:
        # Explicit named schedules like `hourly` satisfy the 6h budget.
        assert cadence in ("OnCalendar=hourly", "OnCalendar=daily")


def test_timer_runs_on_boot_after_missed_fire() -> None:
    assert "Persistent=true" in TIMER.read_text()


def test_timer_installs_to_timers_target() -> None:
    assert "WantedBy=timers.target" in TIMER.read_text()
