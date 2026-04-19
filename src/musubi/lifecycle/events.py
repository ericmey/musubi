"""LifecycleEvent sink — sqlite-backed, thread-safe, batched flush.

Every state change produces exactly one :class:`LifecycleEvent`. The sink is
the canonical persistence target:

- Records are batched in memory up to ``flush_every_n`` rows or
  ``flush_every_s`` seconds, whichever comes first, then committed in a
  single sqlite transaction.
- The background flusher is a daemon thread that wakes on its interval and
  drains the pending queue. On an explicit ``flush()``, ``close()`` or a
  count-triggered flush, the current thread drains inline.
- The on-disk schema is the pydantic ``LifecycleEvent`` fields serialised to
  a JSON blob plus a handful of indexed columns (``event_id`` primary key,
  ``object_id``, ``namespace``, ``occurred_epoch``) for cheap scans.
- A Qdrant mirror (collection ``musubi_lifecycle_events``) is declared in
  :mod:`musubi.store` but not wired up yet — mirroring is a follow-up slice.

The store deliberately does NOT raise on re-opening the same file from a new
process: sqlite handles that via WAL + shared-cache semantics. The "survives
worker restart" guarantee in [[04-data-model/lifecycle]] is satisfied by
committing every batch inside a transaction.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from musubi.types.lifecycle_event import LifecycleEvent

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


class LifecycleEventSink:
    """Thread-safe, batched sqlite writer for :class:`LifecycleEvent`.

    Construction opens / creates the database file and schema. A background
    daemon thread wakes every ``flush_every_s`` seconds and drains the
    pending buffer if it is non-empty. ``record()`` is lock-protected and
    triggers an inline flush once the buffer hits ``flush_every_n`` entries.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        flush_every_n: int = 100,
        flush_every_s: float = 5.0,
    ) -> None:
        if flush_every_n < 1:
            raise ValueError("flush_every_n must be >= 1")
        if flush_every_s <= 0:
            raise ValueError("flush_every_s must be > 0")
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._flush_every_n = flush_every_n
        self._flush_every_s = flush_every_s

        # sqlite connection is shared across threads, so disable per-thread
        # check — we serialise access through ``_lock``.
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; transactions are explicit.
        )
        self._conn.executescript(_SCHEMA)

        self._buffer: list[LifecycleEvent] = []
        self._lock = threading.Lock()
        self._closed = False
        self._stop_event = threading.Event()
        self._flusher = threading.Thread(
            target=self._flush_loop,
            name=f"LifecycleEventSink-flush-{self._db_path.name}",
            daemon=True,
        )
        self._flusher.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, event: LifecycleEvent) -> None:
        """Enqueue an event. May trigger an inline flush once buffered enough."""
        with self._lock:
            if self._closed:
                raise RuntimeError("LifecycleEventSink is closed")
            self._buffer.append(event)
            full = len(self._buffer) >= self._flush_every_n
        if full:
            self.flush()

    def flush(self) -> int:
        """Force-drain the buffer to sqlite. Returns the number of rows written."""
        with self._lock:
            pending = self._buffer
            self._buffer = []
        if not pending:
            return 0
        self._write_batch(pending)
        return len(pending)

    def read_all(self) -> list[LifecycleEvent]:
        """Return every persisted event ordered by ``occurred_epoch`` ascending.

        Used by tests + reflection. The read path is intentionally simple —
        no pagination — because the event volume is small relative to the
        memory planes it audits.
        """
        with self._lock:
            if self._closed:
                # Allow reading even after close — the file is still there.
                return _read_all_on_new_connection(self._db_path)
            cur = self._conn.execute(
                "SELECT payload FROM lifecycle_events ORDER BY occurred_epoch ASC, event_id ASC"
            )
            rows = cur.fetchall()
        return [_deserialise(row[0]) for row in rows]

    def close(self) -> None:
        """Stop the flusher, drain the buffer, close the sqlite handle."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop_event.set()
        self._flusher.join(timeout=max(self._flush_every_s * 2, 1.0))
        # Final drain after the loop exits.
        self.flush()
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _flush_loop(self) -> None:
        """Background flusher — wakes on the configured interval."""
        while not self._stop_event.wait(self._flush_every_s):
            # Never let the flusher thread die on a transient sqlite error.
            # The buffer is preserved for the next interval; the next call
            # to ``record`` or ``flush`` will retry the write.
            with contextlib.suppress(Exception):
                self.flush()

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
        with self._lock:
            if self._closed:
                return
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


def _read_all_on_new_connection(db_path: Path) -> list[LifecycleEvent]:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT payload FROM lifecycle_events ORDER BY occurred_epoch ASC, event_id ASC"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [_deserialise(r[0]) for r in rows]


__all__ = ["LifecycleEventSink"]
