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
from pathlib import Path
from typing import Any

import pytest

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
