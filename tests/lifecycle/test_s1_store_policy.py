"""S1 proof: the post-close read path opens through the shared lifecycle store
policy with the CONFIGURED busy_timeout — not a bare connection.

Covers the shared-connection escape found in S1 review: when a
:class:`~musubi.lifecycle.events.LifecycleEventSink` is closed, ``read_all`` opens a
fresh connection via ``_read_all_on_new_connection``. That connection must come from
``store.connect`` with the sink's configured ``busy_timeout_ms`` (so WAL + busy_timeout
apply on the public read path too), and the events must still read back intact
(behavior-neutral). This is S1 source coverage; it does not touch the frozen C6b
contract or C6 record/flush/close semantics.
"""

from __future__ import annotations

import sqlite3
import types
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import AnyHttpUrl, SecretStr, ValidationError

from musubi.config import Settings
from musubi.lifecycle import store
from musubi.lifecycle.events import LifecycleEventSink
from musubi.types.common import generate_ksuid
from musubi.types.lifecycle_event import LifecycleEvent


def _event(object_id: str) -> LifecycleEvent:
    return LifecycleEvent(
        object_id=object_id,
        object_type="episodic",
        namespace="eric/claude-code/episodic",
        from_state="provisional",
        to_state="matured",
        actor="maturation-worker",
        reason="s1 store-policy proof",
    )


def test_post_close_read_uses_shared_store_policy_with_configured_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = (
        1234  # a genuinely non-default busy_timeout so the spy proves config threads through
    )
    assert configured != store.DEFAULT_BUSY_TIMEOUT_MS

    oid1, oid2 = generate_ksuid(), generate_ksuid()
    sink = LifecycleEventSink(db_path=tmp_path / "events.db", busy_timeout_ms=configured)
    sink.record(_event(oid1))
    sink.record(_event(oid2))
    # Explicit flush BEFORE close: close() is not durable-on-accept (it sets _closed
    # before flush and _write_batch early-returns) — an accepted C6 red we leave FROZEN.
    # We flush here to persist the events, then exercise the post-close READ path.
    sink.flush()
    sink.close()  # after close, read_all() must take the fresh-connection path

    seen_timeouts: list[int] = []
    real_connect = store.connect

    def spy_connect(
        db_path: Path,
        *,
        busy_timeout_ms: int = store.DEFAULT_BUSY_TIMEOUT_MS,
        **kwargs: Any,
    ) -> sqlite3.Connection:
        seen_timeouts.append(busy_timeout_ms)
        return real_connect(db_path, busy_timeout_ms=busy_timeout_ms, **kwargs)

    # events.py imported the ``store`` module, so patching ``store.connect`` is seen there.
    monkeypatch.setattr(store, "connect", spy_connect)
    events = sink.read_all()

    # The post-close read routed through store.connect EXACTLY ONCE with the CONFIGURED
    # (non-default) timeout — exact-list equality so a stray default/bare-fallback call
    # cannot hide beside the correct one.
    assert seen_timeouts == [configured], (
        f"post-close read must open via store.connect exactly once with "
        f"busy_timeout_ms={configured}; saw {seen_timeouts}"
    )

    # Behavior-neutral: both events read back intact through the shared-policy connection.
    assert {str(e.object_id) for e in events} == {str(oid1), str(oid2)}


# ---------------------------------------------------------------------------
# WAL-establishment bounded-retry proofs (store._establish_wal)
# ---------------------------------------------------------------------------


class _BusyError(sqlite3.OperationalError):
    """An OperationalError classified BUSY by sqlite base code (as the C module sets it)."""

    sqlite_errorcode = sqlite3.SQLITE_BUSY


class _FakeConn:
    """Minimal connection double for ``_establish_wal``: the WAL pragma raises the given
    error (or, once ``wal_ok`` is set, reports ``wal``); busy_timeout pragmas are recorded."""

    def __init__(self, wal_error: Exception | None) -> None:
        self._wal_error = wal_error
        self.wal_attempts = 0
        self.busy_timeouts: list[int] = []
        self.wal_ok_after: int | None = None

    def execute(self, sql: str) -> Any:
        if sql.startswith("PRAGMA journal_mode"):
            self.wal_attempts += 1
            if self.wal_ok_after is not None and self.wal_attempts >= self.wal_ok_after:
                return _FakeCursor(("wal",))
            if self._wal_error is not None:
                raise self._wal_error
            return _FakeCursor(("wal",))
        if sql.startswith("PRAGMA busy_timeout"):
            self.busy_timeouts.append(int(sql.split("=", 1)[1]))
        return _FakeCursor(None)


class _FakeCursor:
    def __init__(self, row: tuple[str, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[str, ...] | None:
        return self._row


def test_establish_wal_does_not_retry_a_non_lock_error() -> None:
    # A non-lock OperationalError (no sqlite base code BUSY/LOCKED) must propagate on the
    # first attempt — never retried.
    conn = _FakeConn(sqlite3.OperationalError("disk I/O error"))
    with pytest.raises(sqlite3.OperationalError):
        store._establish_wal(cast(sqlite3.Connection, conn), Path("x.db"), busy_timeout_ms=5000)
    assert conn.wal_attempts == 1


def test_establish_wal_zero_timeout_is_a_single_attempt() -> None:
    # busy_timeout_ms == 0 means exactly one attempt even on a BUSY error — no retry.
    conn = _FakeConn(_BusyError("database is locked"))
    with pytest.raises(store.LifecycleStoreError):
        store._establish_wal(cast(sqlite3.Connection, conn), Path("x.db"), busy_timeout_ms=0)
    assert conn.wal_attempts == 1


def test_establish_wal_retry_respects_total_deadline_without_multiplying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fake clock: sleep advances a virtual monotonic clock, so the bounded retry is
    # deterministic. WAL stays BUSY forever, so establishment must exhaust the TOTAL
    # budget once (not a fresh timeout per attempt) and fail closed.
    clock = [0.0]
    fake_time = types.SimpleNamespace(
        monotonic=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
    )
    monkeypatch.setattr(store, "time", fake_time)

    conn = _FakeConn(_BusyError("database is locked"))
    with pytest.raises(store.LifecycleStoreError):
        store._establish_wal(cast(sqlite3.Connection, conn), Path("x.db"), busy_timeout_ms=100)

    # Total virtual wall-clock consumed never exceeds the configured budget (0.1s): the
    # retry uses ONE shared deadline, not a fresh timeout per attempt.
    assert clock[0] <= 0.1 + 1e-9
    # Every per-retry busy_timeout is capped to the REMAINING budget (<= 100 and strictly
    # decreasing) — a later execute cannot reclaim a fresh full timeout.
    assert conn.busy_timeouts, "expected at least one retry to set a reduced busy_timeout"
    assert all(v <= 100 for v in conn.busy_timeouts)
    assert all(a > b for a, b in zip(conn.busy_timeouts, conn.busy_timeouts[1:], strict=False))


def test_establish_wal_succeeds_after_lock_and_restores_configured_timeout() -> None:
    # BUSY on the first two attempts, then WAL succeeds on the third: establishment must
    # retry, return, and RESTORE the configured busy_timeout (the last recorded value).
    conn = _FakeConn(_BusyError("database is locked"))
    conn.wal_ok_after = 3
    store._establish_wal(cast(sqlite3.Connection, conn), Path("x.db"), busy_timeout_ms=5000)
    assert conn.wal_attempts == 3  # two retries before success
    assert conn.busy_timeouts, "retries must have reduced busy_timeout"
    # Final recorded busy_timeout is restored to the configured value (not a reduced one).
    assert conn.busy_timeouts[-1] == 5000
    assert conn.busy_timeouts[0] < 5000  # the reductions really happened


@pytest.mark.parametrize("bad", [True, False, -1, -1000, 1.5, "5000", None])
def test_connect_rejects_invalid_busy_timeout_before_opening_sqlite(
    tmp_path: Path, bad: object
) -> None:
    # bool / negative / non-int are rejected by validation BEFORE sqlite is opened, so no
    # db file is created — the invalid value never reaches a PRAGMA.
    db_path = tmp_path / "x.db"
    with pytest.raises(store.LifecycleStoreError):
        store.connect(db_path, busy_timeout_ms=cast(int, bad))
    assert not db_path.exists()


def _valid_settings_kwargs(tmp_path: Path) -> dict[str, Any]:
    return {
        "qdrant_host": "qdrant",
        "qdrant_api_key": SecretStr("test-qdrant-key"),
        "tei_dense_url": AnyHttpUrl("http://tei-dense"),
        "tei_sparse_url": AnyHttpUrl("http://tei-sparse"),
        "tei_reranker_url": AnyHttpUrl("http://tei-reranker"),
        "ollama_url": AnyHttpUrl("http://ollama:11434"),
        "embedding_model": "BAAI/bge-m3",
        "sparse_model": "naver/splade-v3",
        "reranker_model": "BAAI/bge-reranker-v2-m3",
        "llm_model": "qwen2.5:7b-instruct-q4_K_M",
        "vault_path": tmp_path / "vault",
        "artifact_blob_path": tmp_path / "artifacts",
        "lifecycle_sqlite_path": tmp_path / "lifecycle" / "work.sqlite",
        "log_dir": tmp_path / "logs",
        "jwt_signing_key": SecretStr("a-very-long-test-signing-key-for-hs256-tokens-32+bytes"),
        "oauth_authority": AnyHttpUrl("https://issuer.example.com/"),
        "musubi_skip_bootstrap": True,
    }


@pytest.mark.parametrize("value", [0, 600_000])
def test_settings_busy_timeout_accepts_in_bounds(tmp_path: Path, value: int) -> None:
    # The bounds are non-vacuous (the frozen P0c test only proves the field EXISTS): 0
    # (waiting disabled) and the upper bound 600000 are accepted.
    settings = Settings.model_validate(
        {**_valid_settings_kwargs(tmp_path), "lifecycle_sqlite_busy_timeout_ms": value}
    )
    assert settings.lifecycle_sqlite_busy_timeout_ms == value


@pytest.mark.parametrize("value", [-1, 600_001])
def test_settings_busy_timeout_rejects_out_of_bounds(tmp_path: Path, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {**_valid_settings_kwargs(tmp_path), "lifecycle_sqlite_busy_timeout_ms": value}
        )
