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

This module owns the shared lifecycle tables: lifecycle events, the maturation and
synthesis cursors, and — as of S2 — the ``lifecycle_outbox`` admission table with its
``ux_active_intent`` partial-unique index (only the columns S2's admission writes/reads).
The reconcile/lease/attempt/``terminal_epoch`` columns and the ``lifecycle_control`` table
are owned by later slices (S4/S6) and are intentionally NOT declared here yet.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Literal

#: Default busy_timeout (ms) applied when a caller does not inject one. Matches the
#: default of ``Settings.lifecycle_sqlite_busy_timeout_ms``; production passes the
#: setting explicitly at the composition sites.
DEFAULT_BUSY_TIMEOUT_MS = 5000

#: The union schema of every component that shares the lifecycle SQLite file. The
#: sink/cursor tables plus the C6b ``lifecycle_outbox`` admission table (S2). The
#: outbox carries only the columns S2's admission writes/reads; the reconcile/lease/
#: cleanup columns and the ``lifecycle_control`` table are added by later slices (S4/S6).
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

CREATE TABLE IF NOT EXISTS lifecycle_outbox (
    operation_key TEXT PRIMARY KEY,
    object_id TEXT,
    collection TEXT,
    target_state TEXT,
    expected_version INTEGER,
    patch_sha TEXT,
    patch_json TEXT,
    intent_digest TEXT,
    state TEXT,
    event_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_intent
    ON lifecycle_outbox (collection, object_id) WHERE state IN ('PENDING','APPLIED');
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

    ``busy_timeout`` is set FIRST, then WAL is established and VERIFIED via
    :func:`_establish_wal` (``PRAGMA journal_mode`` must report ``wal``). Setting
    busy_timeout alone does NOT reliably make a concurrent journal-mode conversion
    wait, so WAL establishment carries a bounded retry within a total wall-clock budget
    derived from ``busy_timeout_ms`` (see :func:`_establish_wal`). Any PRAGMA error or a
    non-WAL result is fail-closed: the connection is closed and the error re-raised.

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
        _establish_wal(conn, db_path, busy_timeout_ms)
    except Exception:
        conn.close()
        raise
    return conn


#: Small bounded backoff (seconds) between WAL-establishment retries, giving a peer
#: process time to win the exclusive journal-mode lock before we re-attempt.
_WAL_RETRY_BACKOFF_S = 0.02


def _establish_wal(conn: sqlite3.Connection, db_path: Path, busy_timeout_ms: int) -> None:
    """Set and verify ``journal_mode=WAL`` with a bounded retry ONLY on SQLITE_BUSY /
    SQLITE_LOCKED during concurrent journal-mode conversion.

    Setting ``PRAGMA busy_timeout`` does not reliably make a concurrent journal-mode
    conversion wait, so the exclusive-lock contention is retried within a TOTAL
    wall-clock budget — a monotonic deadline derived from ``busy_timeout_ms``, NOT a
    fresh per-attempt timeout. Before each retry the connection's busy_timeout is
    lowered to the remaining budget so a later execute cannot multiply the wait, and the
    configured value is restored once WAL succeeds. A ``busy_timeout_ms`` of 0 means a
    single attempt. Errors whose sqlite base code is neither BUSY nor LOCKED are never
    retried. The final mode is verified to be exactly ``wal``. The caller
    (:func:`connect`) closes the connection on any raised error.
    """
    deadline = time.monotonic() + busy_timeout_ms / 1000.0
    reduced = False
    last_exc: sqlite3.OperationalError | None = None
    attempt = 0
    while True:
        if attempt > 0:
            # Retry path. Sleep a small backoff — capped to the remaining budget — then
            # RECHECK the deadline and cap this execute's busy_timeout to the remaining
            # budget, computed immediately before the execute so waits cannot multiply
            # past the total deadline.
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                break
            time.sleep(min(_WAL_RETRY_BACKOFF_S, remaining_s))
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            conn.execute(f"PRAGMA busy_timeout={remaining_ms}")
            reduced = True
        attempt += 1
        try:
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.OperationalError as exc:
            base_code = (getattr(exc, "sqlite_errorcode", 0) or 0) & 0xFF
            if base_code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise  # non-lock error: never retried
            last_exc = exc
            if busy_timeout_ms == 0:
                break  # zero timeout means a single attempt — no retry
            continue
        mode = str(row[0]) if row else ""
        if mode.casefold() != "wal":
            raise LifecycleStoreError(
                f"could not enable WAL on {str(db_path)!r}: journal_mode={mode!r} "
                "(WAL is required for the shared lifecycle store connection policy)"
            )
        if reduced:
            # Restore the operator-visible busy_timeout to the configured value.
            conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        return
    raise LifecycleStoreError(
        f"could not enable WAL on {str(db_path)!r} within {busy_timeout_ms}ms: "
        "journal-mode conversion stayed locked"
    ) from last_exc


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create every shared lifecycle table/index if absent — idempotent and
    cross-process safe under the connection's busy_timeout. No process-local guard."""
    conn.executescript(_LIFECYCLE_SCHEMA)


__all__ = ["DEFAULT_BUSY_TIMEOUT_MS", "LifecycleStoreError", "connect", "ensure_schema"]
