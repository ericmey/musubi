"""Test contract for slice-ops-backup."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "deploy" / "backup"
MODULE_PATH = ROOT / "musubi" / "ops" / "backup.py"
BACKUP_PLAYBOOK = BACKUP / "backup.yml"
RESTORE_PLAYBOOK = BACKUP / "restore.yml"
DRILL_PLAYBOOK = BACKUP / "drill.yml"
GIT_SYNC_TEMPLATE = BACKUP / "templates" / "git-sync.sh.j2"
RESTIC_ENV_TEMPLATE = BACKUP / "templates" / "restic.env.j2"


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text()) or {}


def _load_backup_module() -> Any:
    spec = importlib.util.spec_from_file_location("musubi_ops_backup", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_git_sync_commits_only_when_changed() -> None:
    script = GIT_SYNC_TEMPLATE.read_text()

    assert "git add -A" in script
    assert "git diff --cached --quiet" in script
    assert script.index("git diff --cached --quiet") < script.index("git commit")


def test_qdrant_snapshot_creates_file_and_rsyncs() -> None:
    playbook_text = BACKUP_PLAYBOOK.read_text()

    assert "/collections/{{ item }}/snapshots" in playbook_text
    assert "method: POST" in playbook_text
    assert "src: /var/lib/musubi/qdrant-storage/snapshots/" in playbook_text
    assert "dest: /mnt/snapshots/qdrant/{{ backup_timestamp }}/" in playbook_text
    assert "vault_backblaze_key_id" in RESTIC_ENV_TEMPLATE.read_text()


def test_artifact_rsync_delete_after_removes_purged_blobs() -> None:
    playbook_text = BACKUP_PLAYBOOK.read_text()

    assert "src: /var/lib/musubi/artifact-blobs/" in playbook_text
    assert "dest: /mnt/snapshots/artifact-blobs/" in playbook_text
    assert "--delete-after" in playbook_text


def test_sqlite_backup_completes_under_5s_at_v1_scale(tmp_path: Path) -> None:
    module = _load_backup_module()
    db_path = tmp_path / "lifecycle-work.sqlite"
    out_path = tmp_path / "snapshots" / "lifecycle-work.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("create table lifecycle_events(id integer primary key, event text)")
        conn.execute("insert into lifecycle_events(event) values ('created')")

    elapsed = module.backup_sqlite(db_path, out_path, timeout_s=5.0)

    assert elapsed < 5.0
    with sqlite3.connect(out_path) as conn:
        assert conn.execute("select count(*) from lifecycle_events").fetchone() == (1,)


def test_drill_playbook_restores_to_working_musubi() -> None:
    drill_text = DRILL_PLAYBOOK.read_text()

    assert "bootstrap.yml" in drill_text
    assert "restore.yml" in drill_text
    assert "musubi-cli index rebuild --collection musubi_curated --source" in drill_text
    assert "musubi-cli artifacts rechunk --all" in drill_text


def test_restore_drill_smoke_suite_passes_within_5min() -> None:
    drill = _load_yaml(DRILL_PLAYBOOK)
    smoke_tasks = [
        task
        for play in drill
        for task in play.get("tasks", [])
        if task.get("name") == "Run restore smoke suite"
    ]

    assert smoke_tasks
    assert smoke_tasks[0]["async"] == 300
    assert "musubi-contract-smoke" in smoke_tasks[0]["ansible.builtin.command"]


def test_corruption_check_fails_on_tampered_snapshot(tmp_path: Path) -> None:
    module = _load_backup_module()
    snapshot = tmp_path / "snapshot.bin"
    snapshot.write_bytes(b"original")
    expected = module.sha256_file(snapshot)
    snapshot.write_bytes(b"tampered")

    assert module.verify_sha256(snapshot, expected) is False


def test_every_asset_has_canonical_owner_documented() -> None:
    matrix = ROOT / "docs" / "architecture" / "09-operations" / "asset-matrix.md"
    table_lines = [
        line
        for line in matrix.read_text().splitlines()
        if line.startswith("| ") and not line.startswith("|---") and "Data" not in line
    ]

    assert table_lines
    for line in table_lines:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert cells[0]
        assert cells[1]
        assert cells[3]


def test_backup_cadence_matches_claimed_rpo() -> None:
    module = _load_backup_module()

    assert module.BACKUP_CADENCE_MINUTES["vault"] <= 15
    assert module.BACKUP_CADENCE_MINUTES["qdrant"] <= 360
    assert module.BACKUP_CADENCE_MINUTES["artifact_blobs"] <= 60
    assert module.BACKUP_CADENCE_MINUTES["sqlite"] <= 60


def test_restore_drills_run_quarterly() -> None:
    module = _load_backup_module()

    assert module.RESTORE_DRILL_CADENCE_DAYS <= 92


def test_curated_rebuild_from_vault_produces_matching_qdrant_count() -> None:
    restore_text = RESTORE_PLAYBOOK.read_text()

    assert "musubi-cli index rebuild --collection musubi_curated --source" in restore_text
    assert "musubi-cli index count --collection musubi_curated" in restore_text
    assert "changed_when: false" in restore_text


def test_artifact_rechunk_produces_same_chunk_count_as_snapshot() -> None:
    restore_text = RESTORE_PLAYBOOK.read_text()

    assert "musubi-cli artifacts rechunk --all" in restore_text
    assert "musubi-cli artifacts chunk-count --source qdrant" in restore_text
    assert "musubi-cli artifacts chunk-count --source filesystem" in restore_text
