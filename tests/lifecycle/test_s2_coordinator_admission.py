"""S2 admission-layer proofs for the durable-intent coordinator (client-free).

These exercise ONLY the admission half of ``transition()`` — the paths that resolve at the
durable boundary (operation_key idempotency, the cap gate, single-active-intent, durable-begin
failures, and the before/after ``_pending_commit`` crash seam) BEFORE any Qdrant client is
needed. The full apply + finalize contract (S3) is proven in ``test_s3_coordinator_apply.py``.

Admission failures/faults are driven with ``client=None`` (they return or propagate before the
pre-apply read), and durable PENDING rows are seeded via ``_write_pending`` directly, so no
Qdrant is ever constructed here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from musubi.lifecycle import store
from musubi.lifecycle.coordinator import (
    LifecycleTransitionCoordinator,
    TransitionIntent,
    _CapExceeded,
)
from musubi.types.common import Err, Ok


def _fault_at(target: str) -> Callable[[str], None]:
    """A _checkpoint seam that raises a real sqlite error at exactly one named boundary."""

    def _cp(name: str) -> None:
        if name == target:
            raise sqlite3.OperationalError(f"injected fault at {target}")

    return _cp


def _intent(object_id: str, operation_key: str | None = None) -> TransitionIntent:
    return TransitionIntent(
        collection="musubi_episodic",
        object_id=object_id,
        namespace="tenant/presence",
        expected_version=1,
        target_state="matured",
        actor="t",
        reason="r",
        operation_key=operation_key,
    )


def _coord(db: Path, *, pending_cap: int = 10_000) -> LifecycleTransitionCoordinator:
    return LifecycleTransitionCoordinator(client=None, db_path=db, pending_cap=pending_cap)


def _seed_pending(coord: LifecycleTransitionCoordinator, intent: TransitionIntent, opk: str) -> str:
    """Seed a durable PENDING row via the admission primitive directly (no Qdrant)."""
    coord._write_pending(intent, opk, "ev-" + opk)
    return opk


def _nonterminal_count(db: Path) -> int:
    con = sqlite3.connect(str(db))
    try:
        return int(
            con.execute(
                "SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN ('PENDING','APPLIED')"
            ).fetchone()[0]
        )
    finally:
        con.close()


def _rows_for_object(db: Path, object_id: str) -> list[tuple[str, str]]:
    con = sqlite3.connect(str(db))
    try:
        return [
            (str(k), str(s))
            for k, s in con.execute(
                "SELECT operation_key, state FROM lifecycle_outbox "
                "WHERE object_id = ? AND state IN ('PENDING','APPLIED')",
                (object_id,),
            ).fetchall()
        ]
    finally:
        con.close()


def test_cap_rejects_at_cap(tmp_path: Path) -> None:
    db = tmp_path / "lc.db"
    coord = _coord(db, pending_cap=3)
    for i in range(3):  # fill to exactly the cap via admission-direct writes (distinct objects)
        _seed_pending(coord, _intent(f"o{i}"), f"k{i}")
    # a NEW admission at the cap is cap_exceeded and writes no row — resolved before any client.
    res = coord.transition(_intent("over", operation_key="k-over"))
    assert isinstance(res, Err)
    assert res.error.code == "cap_exceeded"
    assert _nonterminal_count(db) == 3
    assert _rows_for_object(db, "over") == []
    # and the raw admission primitive raises _CapExceeded (writing no row) at the boundary.
    with pytest.raises(_CapExceeded):
        coord._write_pending(_intent("over2"), "k-over2", "ev")


def test_single_active_same_object_rejects(tmp_path: Path) -> None:
    db = tmp_path / "lc.db"
    coord = _coord(db, pending_cap=100)
    _seed_pending(coord, _intent("o1", operation_key="k1"), "k1")
    # a SECOND active intent for the same object (distinct key) -> active_intent_exists (the
    # ux_active_intent partial-unique index), exactly one active intent survives.
    second = coord.transition(_intent("o1", operation_key="k2"))
    assert isinstance(second, Err)
    assert second.error.code == "active_intent_exists"
    assert len(_rows_for_object(db, "o1")) == 1


def test_operation_key_reuse_is_conflict_or_durable_begin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "lc.db"
    coord = _coord(db, pending_cap=100)
    # (a) reusing an operation_key for a DIFFERENT intent resolves at the replay step (idempotency
    # BEFORE the cap / any mutation) as operation_key_conflict — no new row.
    _seed_pending(coord, _intent("oa", operation_key="dup"), "dup")
    conflict = coord.transition(
        _intent("ob", operation_key="dup")
    )  # distinct object -> distinct digest
    assert isinstance(conflict, Err)
    assert conflict.error.code == "operation_key_conflict"
    assert _rows_for_object(db, "ob") == []

    # (b) WARN-2 relocated: a durable-path store.connect that cannot establish the WAL policy raises
    # store.LifecycleStoreError (a RuntimeError, NOT sqlite3.Error) -> durable_begin_failed, no row.
    def _boom(*_a: object, **_k: object) -> object:
        raise store.LifecycleStoreError("injected WAL-policy failure at the durable path")

    monkeypatch.setattr(store, "connect", _boom)
    res = coord.transition(_intent("oc", operation_key="k-oc"))
    monkeypatch.undo()
    assert isinstance(res, Err)
    assert res.error.code == "durable_begin_failed"
    assert _rows_for_object(db, "oc") == []


def test_admission_crash_seam_faults(tmp_path: Path) -> None:
    # WARN-1 relocated. A fault at before_pending_commit is a durable-begin failure (bounded Err,
    # no row). A fault at after_pending_commit is OUTSIDE the durable-begin catch -> it PROPAGATES
    # on an already-committed PENDING row and is NEVER mapped to durable_begin_failed. Both resolve
    # before the pre-apply read, so client=None is never reached.
    before_db = tmp_path / "before.db"
    before = _coord(before_db)
    before._checkpoint = _fault_at("before_pending_commit")
    bres = before.transition(_intent("pre"))
    assert isinstance(bres, Err)
    assert bres.error.code == "durable_begin_failed"
    assert _rows_for_object(before_db, "pre") == []

    after_db = tmp_path / "after.db"
    after = _coord(after_db)
    after._checkpoint = _fault_at("after_pending_commit")
    with pytest.raises(sqlite3.OperationalError):
        after.transition(_intent("post"))
    committed = _rows_for_object(after_db, "post")
    assert len(committed) == 1 and committed[0][1] == "PENDING"


# ---------------------------------------------------------------------------
# Identity is (collection, NAMESPACE, object_id) -- at the schema and the key,
# not only at the lookup (Copilot round 8, musubi#771)
# ---------------------------------------------------------------------------


def _ns_intent(object_id: str, namespace: str) -> TransitionIntent:
    """Two rows that differ ONLY by namespace: the case `object_id` non-uniqueness makes real."""
    return TransitionIntent(
        collection="musubi_episodic",
        object_id=object_id,
        namespace=namespace,
        expected_version=1,
        target_state="matured",
        actor="t",
        reason="r",
    )


def test_two_namespaces_sharing_an_object_id_both_admit(tmp_path: Path) -> None:
    """THE SCHEMA CELL. `ux_active_intent` was unique on `(collection, object_id)`.

    Identity error in a DATABASE CONSTRAINT, not in a lookup: two independent rows that
    merely share an id could not both have an active intent, so a transition in
    namespace B was refused `active_intent_exists` because namespace A had one in
    flight -- permanently, for as long as A's intent stayed PENDING.

    Aoi's framing is why this was missed for eight rounds: the enumeration was "who
    calls this function", and a unique index is not a caller. The right question is
    *everywhere a row is identified*."""
    coord = _coord(tmp_path / "wk.db")
    a = _ns_intent("shared-oid", "tenant/a")
    b = _ns_intent("shared-oid", "tenant/b")

    # EXPLICIT distinct keys. Deriving them would let the canonical-key fix satisfy this
    # cell -- and it did: reverting the KEY made this fail while reverting the INDEX left
    # it green, so it was testing the primary key and never the constraint it names.
    coord._write_pending(a, "explicit-key-a", "ev-a")
    coord._write_pending(b, "explicit-key-b", "ev-b")  # must not raise

    con = store.connect(tmp_path / "wk.db")
    try:
        rows = con.execute(
            "SELECT namespace FROM lifecycle_outbox WHERE object_id=? AND state='PENDING'"
            " ORDER BY namespace",
            ("shared-oid",),
        ).fetchall()
    finally:
        con.close()
    assert [r[0] for r in rows] == ["tenant/a", "tenant/b"], (
        f"expected both namespaces to hold an active intent, got {rows}"
    )


def test_the_canonical_key_separates_two_namespaces(tmp_path: Path) -> None:
    """THE KEY CELL. The derived key was `canon:{collection}:{object_id}:{version}:{state}`.

    Two legitimate rows therefore derived the SAME key and the second was refused
    `operation_key_conflict` -- a different symptom than the index, the same identity
    error one layer up. Both had to be fixed; fixing either alone leaves the other."""
    coord = _coord(tmp_path / "wk.db")
    a = _ns_intent("shared-oid", "tenant/a")
    b = _ns_intent("shared-oid", "tenant/b")

    assert coord._key(a) != coord._key(b), (
        f"both namespaces derive the same canonical key {coord._key(a)!r}; the second "
        f"row can never transition"
    )
    assert "tenant/a" in coord._key(a)
    # The two key SPACES must be disjoint, or a v2 key could collide with some v1 key
    # and silently conflate two different intents.
    assert coord._key(a).startswith("canon2:")
    assert (coord._legacy_key(a) or "").startswith("canon:")


def test_a_legacy_keyed_row_is_replayed_not_duplicated(tmp_path: Path) -> None:
    """THE COMPATIBILITY CELL, and the reason stored keys are not rewritten.

    An outbox row admitted BEFORE namespace entered the canonical key is still in flight
    under its legacy key. A retry of that same intent must replay it; admitting a second
    row would do the work twice. Renaming stored keys instead would race the reconciler
    that holds them, which is why the probe is a read (Tama, musubi#771)."""
    db = tmp_path / "wk.db"
    coord = _coord(db)
    intent = _ns_intent("legacy-oid", "tenant/a")
    legacy_opk = coord._legacy_key(intent)
    assert legacy_opk is not None

    # Seed the row exactly as the pre-namespace code would have admitted it.
    coord._write_pending(intent, legacy_opk, "ev-legacy")

    result = coord.transition(intent)

    con = store.connect(db)
    try:
        keys = [r[0] for r in con.execute("SELECT operation_key FROM lifecycle_outbox").fetchall()]
    finally:
        con.close()
    assert keys == [legacy_opk], (
        f"the retry admitted a SECOND row instead of replaying the legacy one: {keys}"
    )

    # THE ASSERTION THAT MAKES THIS CELL MEAN ANYTHING. "No second row" is satisfied by a
    # REFUSAL as well as by a replay -- the unique index blocks the duplicate either way --
    # so without this the cell passed with the compatibility probe deleted. Measured, not
    # feared: removing the probe left it green.
    #
    # The two outcomes are opposite for the caller. A replay returns the in-flight
    # outcome; `active_intent_exists` tells a legitimate retry its own work is somebody
    # else's and it can never proceed.
    # ASSERT THE OUTCOME, not the absence of a wrong one. "not active_intent_exists"
    # still accepts `operation_key_conflict`, `terminal_apply_failure`, or any future
    # Err -- a cell that excludes one failure is not a cell that requires the success
    # (Yua, musubi#771). The legacy row is PENDING, so a replay is `Ok(TransitionPending)`
    # carrying THAT row's key and event id.
    assert isinstance(result, Ok), f"expected a replay, got {result!r}"
    assert result.value.operation_key == legacy_opk, (
        f"replayed under {result.value.operation_key!r}, not the legacy row's key"
    )
    assert result.value.event_id == "ev-legacy", (
        f"replay returned event_id {result.value.event_id!r}; it is not the in-flight row"
    )


def test_a_legacy_row_from_another_namespace_is_not_claimed(tmp_path: Path) -> None:
    """The probe's safety property, and the positive control for the cell above.

    The legacy key cannot distinguish namespaces -- that is the whole defect -- so the
    probe must not match on the key alone. `intent_digest` already binds namespace, so a
    legacy row belonging to tenant/a cannot be replayed for tenant/b. It is ignored and
    tenant/b admits under its own key; returning a conflict would be the other way to be
    wrong here."""
    db = tmp_path / "wk.db"
    coord = _coord(db)
    a = _ns_intent("shared-oid", "tenant/a")
    b = _ns_intent("shared-oid", "tenant/b")
    legacy_opk = coord._legacy_key(a)
    assert legacy_opk is not None
    assert legacy_opk == coord._legacy_key(b), "the legacy key is namespace-blind by construction"

    coord._write_pending(a, legacy_opk, "ev-legacy-a")
    assert coord._row_for_key(legacy_opk) is not None
    assert coord._intent_digest(a) != coord._intent_digest(b), (
        "the digest does not bind namespace, so the legacy probe cannot be made safe"
    )

    # EXECUTE B. Asserting the digests differ proves a property of the digest, not of the
    # probe -- the probe could still claim or reject tenant B and this cell would stay
    # green. Tama caught that; it is the same inert shape twice in one file.
    result = coord.transition(b)

    con = store.connect(db)
    try:
        rows = dict(con.execute("SELECT operation_key, namespace FROM lifecycle_outbox").fetchall())
    finally:
        con.close()

    assert rows.get(legacy_opk) == "tenant/a", (
        f"tenant/a's in-flight legacy row was disturbed by tenant/b's admission: {rows}"
    )
    assert coord._key(b) in rows and rows[coord._key(b)] == "tenant/b", (
        f"tenant/b did not admit under its own canon2 key; rows={rows}"
    )
    assert not (isinstance(result, Err) and result.error.code == "active_intent_exists"), (
        "tenant/b was refused because tenant/a holds a legacy-keyed intent for a "
        "different namespace; ignoring a non-matching legacy row is the whole point"
    )
    assert not (isinstance(result, Err) and result.error.code == "operation_key_conflict"), (
        "tenant/b was resolved against tenant/a's legacy row and reported a conflict"
    )


def test_ensure_schema_rescopes_an_existing_two_column_index(tmp_path: Path) -> None:
    """THE MIGRATION CELL, and the one a schema edit alone cannot pass.

    `CREATE UNIQUE INDEX IF NOT EXISTS` is a NO-OP when the name already exists, whatever
    its columns. So editing the schema text fixes fresh databases and leaves every
    EXISTING one on the broken two-column index -- green in tests, broken in production,
    which is the worst split available."""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    try:
        con.executescript(
            "CREATE TABLE lifecycle_outbox (operation_key TEXT PRIMARY KEY, object_id TEXT,"
            " collection TEXT, namespace TEXT, state TEXT);"
            "CREATE UNIQUE INDEX ux_active_intent ON lifecycle_outbox (collection, object_id)"
            " WHERE state IN ('PENDING','APPLIED');"
        )
        con.commit()
        before = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_active_intent'"
        ).fetchone()[0]
        assert "namespace" not in before, "the fixture did not create the OLD index"
    finally:
        con.close()

    con = store.connect(db)
    try:
        store.ensure_schema(con)
        after = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_active_intent'"
        ).fetchone()[0]
    finally:
        con.close()

    assert "namespace" in after, (
        f"an existing database kept the two-column index after ensure_schema: {after}"
    )


def test_ambiguous_object_id_is_in_the_documented_error_contract() -> None:
    """The contract is a list callers switch on; a code missing from it is invisible.

    Behaviour changes reach the code and stall at the rendered contract, so this is the
    falsifier for the docstring rather than a note asking someone to remember."""
    from musubi.lifecycle.transitions import TransitionError

    doc = TransitionError.__doc__ or ""
    assert "ambiguous_object_id" in doc, (
        "`ambiguous_object_id` is returned but undocumented; callers cannot distinguish "
        "it from the other refusals"
    )


def test_null_namespace_rows_still_collide(tmp_path: Path) -> None:
    """THE COALESCE CELL, and the one every other namespace cell cannot substitute for.

    All of them write a real namespace, so they stay green with `COALESCE` removed --
    the expression is invisible to a non-NULL row. Its whole purpose is the NULL case:
    SQLite treats NULLs as DISTINCT in a unique index, so a bare three-column index
    would collide with NOTHING for such a row. Widening the constraint would have
    SWITCHED IT OFF exactly where it used to hold, rather than tightening it.

    A NULL namespace should not occur -- admission rejects a falsy one -- but the index
    is the last line, and 'cannot happen' is the reason nobody would have noticed
    (Tama, musubi#771)."""
    db = tmp_path / "wk.db"
    con = store.connect(db)
    try:
        store.ensure_schema(con)
        con.execute(
            "INSERT INTO lifecycle_outbox (operation_key,object_id,collection,namespace,state)"
            " VALUES ('k1','oid','musubi_episodic',NULL,'PENDING')"
        )
        con.commit()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO lifecycle_outbox (operation_key,object_id,collection,namespace,state)"
                " VALUES ('k2','oid','musubi_episodic',NULL,'PENDING')"
            )
            con.commit()
    finally:
        con.close()


def test_ensure_schema_replaces_a_plain_three_column_index(tmp_path: Path) -> None:
    """The migration's own falsifier. A plain three-column index is NOT current.

    The guard read `"namespace" in sql`, so this exact index -- the unsafe one -- was
    accepted as already-migrated and left in place. The cell seeds it directly rather
    than the two-column form, because that is the variant the guard got wrong."""
    db = tmp_path / "plain.db"
    con = sqlite3.connect(db)
    try:
        con.executescript(
            "CREATE TABLE lifecycle_outbox (operation_key TEXT PRIMARY KEY, object_id TEXT,"
            " collection TEXT, namespace TEXT, state TEXT);"
            "CREATE UNIQUE INDEX ux_active_intent ON lifecycle_outbox"
            " (collection, namespace, object_id) WHERE state IN ('PENDING','APPLIED');"
        )
        con.commit()
    finally:
        con.close()

    con = store.connect(db)
    try:
        store.ensure_schema(con)
        sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_active_intent'"
        ).fetchone()[0]
        # ...and it actually constrains NULLs now, which is the point of replacing it.
        con.execute(
            "INSERT INTO lifecycle_outbox (operation_key,object_id,collection,namespace,state)"
            " VALUES ('k1','oid','musubi_episodic',NULL,'PENDING')"
        )
        con.commit()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO lifecycle_outbox (operation_key,object_id,collection,namespace,state)"
                " VALUES ('k2','oid','musubi_episodic',NULL,'PENDING')"
            )
            con.commit()
    finally:
        con.close()

    assert "COALESCE" in sql.upper(), f"the plain three-column index survived ensure_schema: {sql}"


def test_an_index_coalescing_the_wrong_field_is_replaced(tmp_path: Path) -> None:
    """The guard must check the mechanism is applied to the RIGHT OBJECT.

    `"COALESCE" in sql` accepted an index that coalesces the wrong field entirely --
    `COALESCE(collection,'')` contains the word and leaves the NULL hazard on
    `namespace` exactly where it was. Testing for the PRESENCE of a mechanism rather
    than for its correct application is the defect this whole change is about, and it
    reappeared inside the guard written against it (Tama, musubi#771)."""
    db = tmp_path / "wrongfield.db"
    con = sqlite3.connect(db)
    try:
        con.executescript(
            "CREATE TABLE lifecycle_outbox (operation_key TEXT PRIMARY KEY, object_id TEXT,"
            " collection TEXT, namespace TEXT, state TEXT);"
            "CREATE UNIQUE INDEX ux_active_intent ON lifecycle_outbox"
            " (COALESCE(collection, ''), namespace, object_id)"
            " WHERE state IN ('PENDING','APPLIED');"
        )
        con.commit()
    finally:
        con.close()

    con = store.connect(db)
    try:
        store.ensure_schema(con)
        con.execute(
            "INSERT INTO lifecycle_outbox (operation_key,object_id,collection,namespace,state)"
            " VALUES ('k1','oid','musubi_episodic',NULL,'PENDING')"
        )
        con.commit()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO lifecycle_outbox (operation_key,object_id,collection,namespace,state)"
                " VALUES ('k2','oid','musubi_episodic',NULL,'PENDING')"
            )
            con.commit()
    finally:
        con.close()


def test_the_migration_guard_recognises_its_own_index_as_current(tmp_path: Path) -> None:
    """THE GUARD'S OWN FALSIFIER, and the failure mode nothing else here would catch.

    If the expected literal ever stops matching what the schema actually creates, the
    guard matches NOTHING: every `ensure_schema` silently DROPs and re-CREATEs the
    index. Behaviour stays correct, every other cell stays green, and the guard has
    quietly stopped being a guard -- an inert check that cannot fail for the reason it
    exists.

    So assert the two agree, from the live database rather than from the source text."""
    db = tmp_path / "fresh.db"
    con = store.connect(db)
    try:
        store.ensure_schema(con)
        sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_active_intent'"
        ).fetchone()[0]
    finally:
        con.close()

    assert store._EXPECTED_ACTIVE_INTENT_KEY in store._normalise_sql(sql), (
        f"the migration guard's expected key expression no longer matches the index the "
        f"schema creates, so it will re-create on every open and never recognise a "
        f"correct index:\n  expected {store._EXPECTED_ACTIVE_INTENT_KEY!r}\n  actual   "
        f"{store._normalise_sql(sql)!r}"
    )
