"""Shared sqlite-backed write log for echo prevention."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import NamedTuple


class WriteEntry(NamedTuple):
    file_path: str
    body_hash: str
    written_by: str
    written_at: float
    consumed_at: float | None


class WriteLog:
    """Sqlite-backed log of recent vault writes.

    Used to distinguish between human edits (to be indexed) and Core writes
    (to be ignored by the watcher).
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS writes (
                    file_path TEXT NOT NULL,
                    body_hash TEXT NOT NULL,
                    written_by TEXT NOT NULL,
                    written_at REAL NOT NULL,
                    consumed_at REAL DEFAULT NULL,
                    PRIMARY KEY (file_path, body_hash)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_written_at ON writes(written_at)")

    def record_write(self, file_path: str, body_hash: str, written_by: str = "core") -> None:
        """Record a write about to happen."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO writes (file_path, body_hash, written_by, written_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(file_path, body_hash) DO UPDATE SET
                    written_by=excluded.written_by,
                    written_at=excluded.written_at,
                    consumed_at=NULL
                """,
                (file_path, body_hash, written_by, time.time()),
            )

    def consume_if_exists(self, file_path: str, body_hash: str) -> bool:
        """Check if a write exists and consume it if so.

        Returns True if a matching unconsumed core write was found.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT written_by FROM writes
                WHERE file_path = ? AND body_hash = ? AND consumed_at IS NULL
                """,
                (file_path, body_hash),
            )
            row = cursor.fetchone()
            if row and row[0] == "core":
                conn.execute(
                    "UPDATE writes SET consumed_at = ? WHERE file_path = ? AND body_hash = ?",
                    (time.time(), file_path, body_hash),
                )
                return True
        return False

    def purge_old_entries(self, max_age_sec: float = 3600) -> int:
        """Remove entries older than max_age_sec (default 1h)."""
        cutoff = time.time() - max_age_sec
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM writes WHERE written_at < ?", (cutoff,))
            return cursor.rowcount

    def get_orphaned_writes(self, age_sec: float = 300) -> list[WriteEntry]:
        """Return 'core' writes older than age_sec (default 5m) that were never consumed."""
        cutoff = time.time() - age_sec
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT * FROM writes WHERE written_by = 'core' AND consumed_at IS NULL AND written_at < ?",
                (cutoff,),
            )
            return [WriteEntry(*row) for row in cursor.fetchall()]
