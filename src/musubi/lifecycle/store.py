"""Shared lifecycle SQLite store — the single owner of the lifecycle DB's schema
and connection policy.

Every component that opens the shared lifecycle SQLite file
(:class:`~musubi.lifecycle.events.LifecycleEventSink`,
:class:`~musubi.lifecycle.maturation.MaturationCursor`,
:class:`~musubi.lifecycle.synthesis.SynthesisCursor`) acquires its connection here,
so the connection policy — WAL journalling plus an explicit ``busy_timeout`` — is
applied uniformly and the schema is created from ONE place instead of three private
copies.

Connection policy (c6b-phase1-source-cut-plan §D):

- ``PRAGMA busy_timeout`` is set FIRST, before any contended WAL/schema work, so a
  genuine concurrent first-open (two processes creating the same tables) waits a
  bounded time instead of failing immediately with ``SQLITE_BUSY``.
- ``PRAGMA journal_mode=WAL`` — a persistent, database-level mode; setting it on any
  connection makes the file WAL for every opener.
- The schema is ``CREATE TABLE IF NOT EXISTS`` — idempotent and cross-process safe
  under the connection's busy_timeout. There is deliberately NO process-local
  "initialized" flag: repeated :func:`ensure_schema` calls are cheap no-ops.

Callers keep their own ``isolation_level`` / ``check_same_thread``: the sink uses a
single cross-thread autocommit connection with explicit ``BEGIN IMMEDIATE`` batches;
the cursors use the sqlite3 default (deferred) per-operation connection. The policy
layer changes ONLY the journal mode and busy_timeout, preserving each caller's
existing transaction semantics.

The default busy_timeout is 5000 ms. Production composition passes
``settings.lifecycle_sqlite_busy_timeout_ms`` at the sink/cursor construction sites;
direct callers (tests) inherit the default. A configured value of ``0`` disables
waiting — SQLite returns ``SQLITE_BUSY`` immediately on contention rather than
blocking; it is a deliberate operator override, not a fail-closed guard.

This module owns ONLY today's tables (lifecycle events plus the maturation and
synthesis cursors). ``lifecycle_outbox`` / ``lifecycle_control`` belong to a later
slice (S2) and are intentionally NOT declared here.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

#: Default busy_timeout (ms) applied when a caller does not inject one. Matches the
#: default of ``Settings.lifecycle_sqlite_busy_timeout_ms``; production passes the
#: setting explicitly at the composition sites.
DEFAULT_BUSY_TIMEOUT_MS = 5000

#: The union schema of every component that shares the lifecycle SQLite file — exactly
#: the tables/indexes those components create today, nothing more. (lifecycle_outbox /
#: lifecycle_control belong to S2 and are NOT declared here.)
_LIFECYCLE_SCHEMA = """
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

CREATE TABLE IF NOT EXISTS maturation_cursor (
    sweep_name TEXT PRIMARY KEY,
    last_processed_epoch REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS synthesis_cursor (
    namespace TEXT PRIMARY KEY,
    last_processed_epoch REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS synthesis_family_cursor (
    identity_family TEXT PRIMARY KEY,
    last_processed_epoch REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS synthesis_candidates (
    identity_family TEXT NOT NULL,
    memory_object_id TEXT NOT NULL,
    first_seen_epoch REAL NOT NULL,
    last_attempt_epoch REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (identity_family, memory_object_id)
);
CREATE INDEX IF NOT EXISTS idx_candidates_age
    ON synthesis_candidates(identity_family, first_seen_epoch);
"""


class LifecycleStoreError(RuntimeError):
    """Raised when the shared lifecycle connection policy cannot be established
    (invalid busy_timeout, or WAL could not be confirmed on the connection)."""


def connect(
    db_path: Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None = "DEFERRED",
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open a connection to the shared lifecycle DB with the uniform policy applied.

    ``busy_timeout`` is set FIRST, before the WAL pragma (a brief write lock) or any
    schema DDL, so a concurrent first-open under contention waits a bounded
    ``busy_timeout_ms`` rather than failing. WAL is then set AND VERIFIED
    (``PRAGMA journal_mode`` must report ``wal``); the busy_timeout supplies the whole
    contention budget, so there is no unbounded retry and no false "we were first"
    assumption. Any PRAGMA error or a non-WAL result is fail-closed: the connection is
    closed and the error re-raised.

    The caller's ``isolation_level`` and ``check_same_thread`` are passed straight
    through so its transaction semantics are unchanged.
    """
    # ``bool`` is a subclass of ``int`` — reject it explicitly so ``True``/``False``
    # cannot be interpolated into the PRAGMA and silently alter the policy.
    if (
        isinstance(busy_timeout_ms, bool)
        or not isinstance(busy_timeout_ms, int)
        or busy_timeout_ms < 0
    ):
        raise LifecycleStoreError(
            f"busy_timeout_ms must be a non-negative int, got {busy_timeout_ms!r}"
        )
    conn = sqlite3.connect(
        str(db_path),
        isolation_level=isolation_level,
        check_same_thread=check_same_thread,
    )
    try:
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        mode = str(row[0]) if row else ""
        if mode.casefold() != "wal":
            raise LifecycleStoreError(
                f"could not enable WAL on {str(db_path)!r}: journal_mode={mode!r} "
                "(the busy_timeout already bounds the contention wait; WAL is required "
                "for the shared lifecycle store connection policy)"
            )
    except Exception:
        conn.close()
        raise
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create every shared lifecycle table/index if absent — idempotent and
    cross-process safe under the connection's busy_timeout. No process-local guard."""
    conn.executescript(_LIFECYCLE_SCHEMA)


__all__ = ["DEFAULT_BUSY_TIMEOUT_MS", "LifecycleStoreError", "connect", "ensure_schema"]
