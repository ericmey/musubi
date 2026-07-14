"""LifecycleEvent sink — sqlite-backed, thread-safe, durable on acceptance.

Every state change produces exactly one :class:`LifecycleEvent`. The sink is
the canonical persistence target:

- ``record()`` commits synchronously and returns ``Ok`` only after SQLite
  commits the event. A failed write is refused as a typed ``Err``.
- There is no in-memory retry queue or background flusher. ``flush()`` remains
  a compatibility no-op for callers written against the previous API.
- The on-disk schema is the pydantic ``LifecycleEvent`` fields serialised to
  a JSON blob plus a handful of indexed columns (``event_id`` primary key,
  ``object_id``, ``namespace``, ``occurred_epoch``) for cheap scans.
- A Qdrant mirror (collection ``musubi_lifecycle_events``) is declared in
  :mod:`musubi.store` but not wired up yet — mirroring is a follow-up slice.

The store deliberately does NOT raise on re-opening the same file from a new
process: SQLite coordinates access to the file. The "survives worker restart"
guarantee in [[04-data-model/lifecycle]] is satisfied by committing every event
inside a transaction before returning ``Ok``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from musubi.observability.registry import default_registry
from musubi.types.common import Err, Ok
from musubi.types.lifecycle_event import LifecycleEvent

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lifecycle_events (
    event_id TEXT PRIMARY KEY,
    object_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    object_type TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    occurred_epoch REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_object ON lifecycle_events (object_id);
CREATE INDEX IF NOT EXISTS idx_events_ns_epoch ON lifecycle_events (namespace, occurred_epoch);
"""

_DEFAULT_BUSY_TIMEOUT_MS = 5_000

_WRITE_FAILURES = default_registry().counter(
    "musubi_lifecycle_event_write_failures_total",
    "Lifecycle event writes refused because SQLite persistence failed.",
)


@dataclass(frozen=True)
class LifecycleEventWriteError:
    """Bounded public error returned when an event could not be committed."""

    code: str = "lifecycle_event_write_failed"


class LifecycleEventSink:
    """Thread-safe synchronous sqlite writer for :class:`LifecycleEvent`.

    ``flush_every_n`` and ``flush_every_s`` remain validated constructor
    arguments for compatibility, but they no longer control durability.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        flush_every_n: int = 100,
        flush_every_s: float = 5.0,
        busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        if flush_every_n < 1:
            raise ValueError("flush_every_n must be >= 1")
        if flush_every_s <= 0:
            raise ValueError("flush_every_s must be > 0")
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._flush_every_n = flush_every_n
        self._flush_every_s = flush_every_s
        # Retained so the post-close read path (``read_all`` -> a fresh connection)
        # opens through the same shared-store policy, not a bare connection.
        self._busy_timeout_ms = busy_timeout_ms

        # Keep the existing standalone event-store boundary on main. C6b owns
        # the later migration to the shared lifecycle store; C6 only changes
        # acceptance durability and caller error propagation.
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; transactions are explicit.
        )
        self._conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        self._conn.executescript(_SCHEMA)

        self._lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, event: LifecycleEvent) -> Ok[None] | Err[LifecycleEventWriteError]:
        """Commit ``event`` synchronously; accept it only after SQLite COMMIT."""
        with self._lock:
            if self._closed:
                return self._write_failure()
            try:
                self._write_batch([event])
            except Exception:
                return self._write_failure()
            return Ok(value=None)

    @staticmethod
    def _write_failure() -> Err[LifecycleEventWriteError]:
        """Return the bounded refusal and emit exactly one PII-free signal."""
        _WRITE_FAILURES.inc()
        log.error("Lifecycle event persistence failed")
        return Err(error=LifecycleEventWriteError())

    def flush(self) -> int:
        """Compatibility no-op: successful records are already committed."""
        return 0

    def read_all(self) -> list[LifecycleEvent]:
        """Return every persisted event ordered by ``occurred_epoch`` ascending.

        Used by tests + reflection. The read path is intentionally simple —
        no pagination — because the event volume is small relative to the
        memory planes it audits.
        """
        with self._lock:
            if self._closed:
                # Allow reading even after close — the file is still there.
                return _read_all_on_new_connection(self._db_path, self._busy_timeout_ms)
            cur = self._conn.execute(
                "SELECT payload FROM lifecycle_events ORDER BY occurred_epoch ASC, event_id ASC"
            )
            rows = cur.fetchall()
        return [_deserialise(row[0]) for row in rows]

    def close(self) -> None:
        """Close the sqlite handle idempotently after any in-flight record."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_batch(self, batch: list[LifecycleEvent]) -> None:
        rows: list[tuple[Any, ...]] = []
        for ev in batch:
            if ev.occurred_epoch is None:
                # Validator fills this in, but belt-and-suspenders: fall back
                # to the event's ``occurred_at`` if it was round-tripped through
                # a serializer that stripped the epoch.
                rows.append(
                    (
                        ev.event_id,
                        ev.object_id,
                        ev.namespace,
                        ev.object_type,
                        ev.from_state,
                        ev.to_state,
                        ev.occurred_at.timestamp(),
                        _serialise(ev),
                    )
                )
            else:
                rows.append(
                    (
                        ev.event_id,
                        ev.object_id,
                        ev.namespace,
                        ev.object_type,
                        ev.from_state,
                        ev.to_state,
                        ev.occurred_epoch,
                        _serialise(ev),
                    )
                )
        # ``record`` holds ``_lock`` for the complete transaction, composing
        # safely with concurrent ``close`` and other records.
        if self._closed:
            raise RuntimeError("LifecycleEventSink is closed")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.executemany(
                "INSERT OR REPLACE INTO lifecycle_events "
                "(event_id, object_id, namespace, object_type, "
                " from_state, to_state, occurred_epoch, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __enter__(self) -> LifecycleEventSink:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        with contextlib.suppress(Exception):
            self.close()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _serialise(event: LifecycleEvent) -> str:
    return event.model_dump_json()


def _deserialise(payload: str) -> LifecycleEvent:
    data = json.loads(payload)
    # The occurred_at timestamp round-trips as an ISO 8601 string.
    raw = data.get("occurred_at")
    if isinstance(raw, str):
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        data["occurred_at"] = parsed
    return LifecycleEvent.model_validate(data)


def _read_all_on_new_connection(db_path: Path, busy_timeout_ms: int) -> list[LifecycleEvent]:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        cur = conn.execute(
            "SELECT payload FROM lifecycle_events ORDER BY occurred_epoch ASC, event_id ASC"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [_deserialise(r[0]) for r in rows]


__all__ = ["LifecycleEventSink", "LifecycleEventWriteError"]
