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
synthesis cursors, the ``lifecycle_outbox`` admission table (S2) with its ``ux_active_intent``
partial-unique index, the S3 ``event_payload``/``terminal_epoch`` columns + the
``lifecycle_apply_markers`` table, and — as of S4 — the outbox's
``lease_owner``/``lease_expires_epoch``/``attempts``/``next_attempt_epoch``/``failure_class``
reconciliation columns. As of S6, the single-row ``lifecycle_control`` table durably coordinates
maintenance/rollback generations; S6 also owns terminal-row cleanup/backfill (not the S3
``terminal_epoch`` nor the S4 lease/attempt/``failure_class`` columns). S5 owns the
emission/metrics/logs of ``failure_class`` — not the column, which S4 persists for R15.

S3 note: the coordinator's ``_finalize`` writes the C6-owned ``lifecycle_events`` table
DIRECTLY (all 8 columns, in the same txn as the outbox APPLIED→FINAL move) rather than
through the buffered ``LifecycleEventSink`` — this is required for R8 atomicity. The
coordinator path owns its own FINAL event; ``events.py`` is unchanged and its sink callers
are not composed on this path (Yua S3 ruling 1). See the slice boundary note.
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
#: sink/cursor tables plus the C6b ``lifecycle_outbox`` (admission S2 + apply/finalize S3
#: [``event_payload`` persisted pre-mutation, ``terminal_epoch`` on FINAL/ABANDONED] +
#: reconciliation S4 [``lease_owner``/``lease_expires_epoch`` for atomic guarded leases,
#: ``attempts``/``next_attempt_epoch`` for durable retry backoff, ``failure_class`` for the
#: durable terminal/transient/unknown classification R15 requires, and ``namespace``/``actor``/
#: ``reason`` — the admission-truth reconstruct columns so a PENDING row is self-sufficient for the
#: server-fenced reapply + event rebuild even before ``event_payload`` exists, e.g. an R5 crash])
#: and the S3
#: ``lifecycle_apply_markers`` effective-apply table. The cleanup/backfill logic and
#: ``lifecycle_control`` are owned by S6 (cleanup/backfill/control), which does NOT own the
#: ``terminal_epoch`` [S3] or the lease/attempt/``failure_class`` columns [S4]. S5 owns the
#: emission/metrics/logs OF ``failure_class`` — not the column itself.
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
    namespace TEXT,
    actor TEXT,
    reason TEXT,
    patch_sha TEXT,
    patch_json TEXT,
    intent_digest TEXT,
    state TEXT,
    event_id TEXT,
    event_payload TEXT,
    terminal_epoch REAL,
    lease_owner TEXT,
    lease_expires_epoch REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_epoch REAL,
    failure_class TEXT,
    intent_kind TEXT
);
-- Active-intent uniqueness is scoped to the FULL identity. `object_id` is not
-- globally unique -- the same id legitimately exists under a different namespace --
-- so a `(collection, object_id)` index rejected two independent rows' concurrent
-- transitions as `active_intent_exists` and one of them could never proceed
-- (Copilot, musubi#771). See `_migrate_active_intent_index` for existing DBs.
-- COALESCE is load-bearing, not defensive noise. SQLite treats NULLs as DISTINCT in a
-- unique index, so a single row with a NULL namespace would not collide with ANYTHING --
-- widening the index from two columns to three would silently switch the constraint OFF
-- for such rows instead of tightening it. Folding NULL onto '' keeps legacy rows
-- constrained exactly as they were before, so the widening can only ever add precision.
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_intent
    ON lifecycle_outbox (collection, COALESCE(namespace, ''), object_id)
    WHERE state IN ('PENDING','APPLIED');

CREATE TABLE IF NOT EXISTS lifecycle_apply_markers (
    operation_key TEXT PRIMARY KEY,
    object_id TEXT,
    target_state TEXT
);

CREATE TABLE IF NOT EXISTS lifecycle_control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    maintenance_active INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO lifecycle_control (id, maintenance_active, generation)
    VALUES (1, 0, 0);
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
    # Additive backward-safe migration (C4/ART-001): older lifecycle DBs predate the
    # ``intent_kind`` discriminator. ADD COLUMN if absent so an existing outbox keeps working;
    # a NULL intent_kind is read as the default lifecycle-transition kind.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(lifecycle_outbox)").fetchall()}
    if "intent_kind" not in cols:
        try:
            conn.execute("ALTER TABLE lifecycle_outbox ADD COLUMN intent_kind TEXT")
        except sqlite3.OperationalError as exc:
            # Two-process race: another process ADD COLUMN'd intent_kind between our PRAGMA read and
            # this ALTER. Tolerate ONLY the duplicate-column error; NEVER mask any other
            # OperationalError (a genuine schema/lock fault must still surface).
            if "duplicate column name" not in str(exc).lower():
                raise
    # End the schema-setup transaction BEFORE the migration, which owns its own.
    conn.commit()
    _migrate_active_intent_index(conn)


def _normalise_sql(sql: str) -> str:
    """Lowercase with all whitespace removed, so the comparison is about STRUCTURE.

    SQLite stores an index definition as written, so formatting differences are not
    evidence of anything -- matching on the raw text would make the guard depend on how
    somebody typed it."""
    return "".join(sql.lower().split())


_EXPECTED_ACTIVE_INTENT_KEY = "(collection,coalesce(namespace,''),object_id)"
"""The exact ordered key expression ``ux_active_intent`` must have.

Checking for the WORD `coalesce` was not enough: an index that coalesces the wrong
field -- `COALESCE(collection,'')` -- contains it and would be accepted as current,
leaving the NULL hazard exactly where it was. Testing for the presence of a mechanism
instead of the mechanism being applied TO THE RIGHT OBJECT is the defect this whole
change is about, and it reappeared inside the guard against it (Tama, musubi#771)."""


def _migrate_active_intent_index(conn: sqlite3.Connection) -> None:
    """Re-scope ``ux_active_intent`` to the full identity, atomically.

    `CREATE UNIQUE INDEX IF NOT EXISTS` is a NO-OP when an index of that name already
    exists, whatever its columns -- so a schema edit alone silently leaves every
    pre-existing database on the old index. The defect would persist exactly where it
    matters (deployments with data) and vanish in tests (fresh DBs).

    **The lock is taken BEFORE the shape is read, and both statements share it.** Two
    faults, and the second is the one that bites in production:

    1. Deciding from an unlocked `sqlite_master` read lets two processes both see the
       old shape and both migrate, the second acting on a decision already stale.
    2. `DROP INDEX` outside a transaction COMMITS. Between it and the `CREATE`, the
       partial unique index does not exist at all -- and any admission landing in that
       window can insert a second active intent for one identity. A migration that
       installs a constraint must never open a hole in it (Copilot/Yua, musubi#771).

    Widening a unique index can never fail on existing rows: more columns means fewer
    collisions, so any set legal under the old key is legal under the new one.
    """
    # This function OWNS its transaction from BEGIN to COMMIT and must be entered with
    # none open. Committing a caller's in-flight transaction from in here would durably
    # land unrelated work as a side effect of a schema check -- the caller ends its own
    # (Yua, musubi#771).
    if conn.in_transaction:
        raise LifecycleStoreError(
            "_migrate_active_intent_index requires no open transaction; the caller must "
            "commit or roll back its own work before the index is replaced"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_active_intent'"
        ).fetchone()
        if (
            row is not None
            and row[0] is not None
            and _EXPECTED_ACTIVE_INTENT_KEY not in _normalise_sql(str(row[0]))
        ):
            conn.execute("DROP INDEX ux_active_intent")
            conn.execute(
                "CREATE UNIQUE INDEX ux_active_intent ON lifecycle_outbox "
                "(collection, COALESCE(namespace, ''), object_id) "
                "WHERE state IN ('PENDING','APPLIED')"
            )
        # COMMIT belongs INSIDE the try. A commit can fail -- disk full, I/O error, a
        # lock lost -- and outside it that exception escapes with no rollback, leaving
        # the connection in-transaction for whatever runs next. The rollback is guarded
        # on `in_transaction` because after a failed COMMIT there may be nothing left to
        # roll back, and an unconditional ROLLBACK would then raise over the real error
        # (Yua, musubi#771).
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


__all__ = ["DEFAULT_BUSY_TIMEOUT_MS", "LifecycleStoreError", "connect", "ensure_schema"]
