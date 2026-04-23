"""Structural tests for deploy/backup/restore.yml and drill.yml.

Closes #190. The playbook rotted when the backup script switched to
``/var/lib/musubi/backups/<TIMESTAMP>/`` and when systemd-native
Musubi was replaced by docker-compose; restore.yml was still targeting
``/mnt/snapshots/``, calling a non-existent ``musubi-cli``, and
restarting a systemd unit that no longer exists.

These tests lock in the acceptance criteria from the issue:

- restore.yml sources from ``/var/lib/musubi/backups/``, never from
  the old ``/mnt/snapshots/`` path.
- restore.yml uses the Qdrant HTTP API to recover snapshots, not a
  phantom CLI.
- restore.yml uses ``community.docker.docker_compose_v2`` to restart
  services, not ``ansible.builtin.systemd_service``.
- drill.yml's bootstrap step is guarded by ``drill_fresh_host`` so
  the default invocation is safe against an already-bootstrapped
  host (e.g. the live musubi.mey.house box).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
RESTORE_YML = ROOT / "deploy" / "backup" / "restore.yml"
DRILL_YML = ROOT / "deploy" / "backup" / "drill.yml"


def _load_multi(path: Path) -> list[Any]:
    """Ansible playbooks are lists of plays at top level; some (like
    drill.yml) also use ``import_playbook`` which yaml parses as a
    dict-in-list. safe_load returns whatever it finds — we only need
    the raw text for assertions below, but this gives a shape check."""
    return list(yaml.safe_load_all(path.read_text()))


# ---------------------------------------------------------------------------
# Parse coverage
# ---------------------------------------------------------------------------


def test_restore_yml_parses_as_yaml() -> None:
    """Broken YAML = broken runtime, catch it at test-time."""
    docs = _load_multi(RESTORE_YML)
    assert docs, "restore.yml is empty"
    plays = docs[0]
    assert isinstance(plays, list) and plays, "restore.yml must be a list of plays"


def test_drill_yml_parses_as_yaml() -> None:
    docs = _load_multi(DRILL_YML)
    assert docs, "drill.yml is empty"


# ---------------------------------------------------------------------------
# Path hygiene — sources must be /var/lib/musubi/backups, never /mnt/snapshots
# ---------------------------------------------------------------------------


def test_restore_yml_sources_from_var_lib_musubi_backups() -> None:
    """The canonical backup layout (per `musubi-backup.sh`) writes
    under ``/var/lib/musubi/backups/<TIMESTAMP>/``. restore.yml must
    read from the same root, not the legacy ``/mnt/snapshots``."""
    text = RESTORE_YML.read_text()
    assert "/var/lib/musubi/backups" in text
    assert "/mnt/snapshots" not in text, (
        "restore.yml still references the legacy /mnt/snapshots path — "
        "the live backup script writes under /var/lib/musubi/backups/"
    )


def test_restore_yml_does_not_rsync_cursor_subdir() -> None:
    """The backup script does NOT create a ``cursors/`` subdirectory,
    so restore.yml mustn't try to rsync one back. Either add it to
    the backup script (separate design call) or leave it out."""
    text = RESTORE_YML.read_text()
    assert "/cursors/" not in text
    assert "cursors/" not in text.replace("/mnt/snapshots/cursors/", "")


# ---------------------------------------------------------------------------
# No phantom CLIs
# ---------------------------------------------------------------------------


def test_restore_yml_does_not_invoke_musubi_cli() -> None:
    """The core image is uvicorn-only — there is no ``musubi-cli``
    binary. The old playbook invoked it for snapshot recovery, index
    rebuild, and artifact rechunk. All of those paths are replaced
    by direct Qdrant API calls / pipeline runs.

    We only flag non-comment occurrences so the explanatory docstring
    in the file is allowed to reference the historical shape."""
    lines = [
        line for line in RESTORE_YML.read_text().splitlines() if not line.lstrip().startswith("#")
    ]
    executable = "\n".join(lines)
    assert "musubi-cli" not in executable, (
        "restore.yml still shells out to `musubi-cli`, but no such "
        "binary exists in the core image. Use Qdrant's HTTP API instead."
    )


def test_restore_yml_uses_qdrant_http_api_for_snapshot_recover() -> None:
    """Qdrant's supported snapshot-restore path is
    ``POST /collections/<name>/snapshots/upload`` or the companion
    ``PUT /collections/<name>/snapshots/recover``. One of those must
    appear in the playbook."""
    text = RESTORE_YML.read_text()
    assert "/snapshots/recover" in text or "/snapshots/upload" in text, (
        "restore.yml must use Qdrant's snapshot-recover/upload HTTP API"
    )


# ---------------------------------------------------------------------------
# Service restart — docker-compose, not systemd
# ---------------------------------------------------------------------------


def test_restore_yml_restarts_via_docker_compose_not_systemd() -> None:
    """Deployment is compose-based (services live as containers
    under a compose project at ``/etc/musubi``). A ``systemd_service:
    name=musubi`` call would fail with "unit not found" on the live
    host."""
    text = RESTORE_YML.read_text()
    assert "community.docker.docker_compose_v2" in text
    assert "systemd_service" not in text, (
        "restore.yml still uses systemd_service to stop/start Musubi — "
        "the stack runs under docker-compose, not a systemd unit."
    )


# ---------------------------------------------------------------------------
# Drill safety — bootstrap must be opt-in
# ---------------------------------------------------------------------------


def test_drill_yml_bootstrap_is_guarded_by_drill_fresh_host() -> None:
    """bootstrap.yml reinstalls packages and creates users — running
    it against an already-bootstrapped host is destructive. The
    default drill invocation must skip bootstrap; fresh-host drills
    must pass ``-e drill_fresh_host=true`` to opt in."""
    text = DRILL_YML.read_text()
    assert "drill_fresh_host" in text, (
        "drill.yml must gate the bootstrap step behind a "
        "`drill_fresh_host` flag (default false) so the default "
        "invocation doesn't rebuild the host."
    )
    assert "import_playbook: ../ansible/bootstrap.yml" in text
    # Whatever the task shape looks like, the bootstrap step must sit
    # behind a conditional that references the flag.
    bootstrap_idx = text.index("import_playbook: ../ansible/bootstrap.yml")
    nearby = text[max(0, bootstrap_idx - 200) : bootstrap_idx + 300]
    assert "drill_fresh_host" in nearby, (
        "drill.yml imports bootstrap.yml without a visible drill_fresh_host "
        "guard — the conditional must be on the same task / play as the import."
    )


def test_drill_yml_validates_after_restore() -> None:
    """A drill without a post-restore health check doesn't actually
    exercise anything — it just copies files around. The playbook
    should hit /v1/ops/health after the restore completes."""
    text = DRILL_YML.read_text()
    assert "/v1/ops/health" in text
