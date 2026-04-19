"""Small backup helpers used by the ops playbooks and tests."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

BACKUP_CADENCE_MINUTES: dict[str, int] = {
    "vault": 15,
    "qdrant": 360,
    "artifact_blobs": 60,
    "sqlite": 60,
}
RESTORE_DRILL_CADENCE_DAYS = 90


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str) -> bool:
    return sha256_file(path) == expected


def backup_sqlite(source: Path, destination: Path, *, timeout_s: float = 5.0) -> float:
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")

    started = time.monotonic()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    elapsed = time.monotonic() - started
    if elapsed > timeout_s:
        raise TimeoutError(f"sqlite backup exceeded {timeout_s}s")
    return elapsed
