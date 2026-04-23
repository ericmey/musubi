"""Tests for scripts/perf/seed_corpus.py helpers.

Covers three behaviours the seed script promises the caller:

1. **Timestamp determinism** — same --seed produces same timestamps,
   across runs and across restarts. This is the property that makes
   the seed idempotent-on-retry.

2. **Idempotency-key stability** — the SHA-derived Idempotency-Key is
   purely a function of (namespace, content, timestamp). Re-running
   the same seed against a Musubi that already has the corpus must
   produce the same keys so the server dedupes.

3. **429 / Retry-After backoff** — honors Retry-After when present,
   falls back to exponential backoff with jitter otherwise, gives up
   after MAX_429_RETRIES consecutive 429s.
"""

from __future__ import annotations

import importlib.util
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
SEED_PATH = ROOT / "scripts" / "perf" / "seed_corpus.py"


def _load() -> Any:
    # Register in sys.modules before exec so dataclass ClassVar
    # resolution can find the module via cls.__module__ lookup.
    if "musubi_perf_seed" in sys.modules:
        return sys.modules["musubi_perf_seed"]
    spec = importlib.util.spec_from_file_location("musubi_perf_seed", SEED_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["musubi_perf_seed"] = module
    spec.loader.exec_module(module)
    return module


def test_timestamp_anchor_is_fixed_epoch() -> None:
    seed = _load()
    anchor: datetime = seed.TIMESTAMP_ANCHOR
    assert anchor == datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)


def test_make_timestamp_is_deterministic_for_same_seed() -> None:
    seed = _load()
    r1 = random.Random(42)
    r2 = random.Random(42)
    a = [seed.make_timestamp(r1, 90) for _ in range(25)]
    b = [seed.make_timestamp(r2, 90) for _ in range(25)]
    assert a == b


def test_make_timestamp_never_exceeds_anchor() -> None:
    seed = _load()
    rng = random.Random(1)
    for _ in range(200):
        ts = seed.make_timestamp(rng, 90)
        assert ts <= seed.TIMESTAMP_ANCHOR


def test_idempotency_key_is_stable() -> None:
    seed = _load()
    # Canonical namespace format is tenant/presence/plane — use a valid
    # shape even in a hashing-only test so the fixture matches what
    # real requests will carry.
    ns = "perf-test/harness/episodic"
    content = "Eric prefers coffee black, no sugar."
    ts = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
    k1 = seed.make_idempotency_key(ns, content, ts)
    k2 = seed.make_idempotency_key(ns, content, ts)
    assert k1 == k2
    assert k1.startswith("perf-seed:")


def test_idempotency_key_changes_with_any_input() -> None:
    seed = _load()
    ts = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
    k_base = seed.make_idempotency_key("t/p/a", "content", ts)
    assert k_base != seed.make_idempotency_key("t/p/b", "content", ts)
    assert k_base != seed.make_idempotency_key("t/p/a", "other content", ts)
    assert k_base != seed.make_idempotency_key(
        "t/p/a", "content", datetime(2026, 3, 2, 12, 0, 0, tzinfo=UTC)
    )


def test_sleep_for_429_honors_retry_after_header(monkeypatch: Any) -> None:
    seed = _load()
    monkeypatch.setattr(seed.time, "sleep", lambda _s: None)
    resp = MagicMock()
    resp.headers = {"Retry-After": "0.1"}
    slept = seed._sleep_for_429(resp, attempt=0, rng=random.Random(0))
    assert slept == 0.1


def test_sleep_for_429_retry_after_is_capped(monkeypatch: Any) -> None:
    seed = _load()
    monkeypatch.setattr(seed.time, "sleep", lambda _s: None)
    resp = MagicMock()
    resp.headers = {"Retry-After": "9999"}
    slept = seed._sleep_for_429(resp, attempt=0, rng=random.Random(0))
    assert slept == seed.MAX_BACKOFF_S


def test_sleep_for_429_falls_back_to_exponential_backoff(monkeypatch: Any) -> None:
    seed = _load()
    monkeypatch.setattr(seed.time, "sleep", lambda _s: None)
    resp = MagicMock()
    resp.headers = {}
    # attempt=2 → 0.25 * 4 = 1.0s + up to 25% jitter → [1.0, 1.25]
    slept = seed._sleep_for_429(resp, attempt=2, rng=random.Random(0))
    assert 1.0 <= slept <= 1.25


def test_post_with_backoff_returns_success_on_2xx(monkeypatch: Any) -> None:
    seed = _load()
    # Skip real sleeps entirely so the test is fast.
    monkeypatch.setattr(seed.time, "sleep", lambda _s: None)

    success = MagicMock()
    success.status_code = 201
    client = MagicMock()
    client.post.return_value = success

    got = seed.post_with_backoff(client, "/memories", rng=random.Random(0), json_body={})
    assert got is success
    assert client.post.call_count == 1


def test_post_with_backoff_retries_429_then_succeeds(monkeypatch: Any) -> None:
    seed = _load()
    monkeypatch.setattr(seed.time, "sleep", lambda _s: None)

    r429 = MagicMock()
    r429.status_code = 429
    r429.headers = {"Retry-After": "0"}
    r200 = MagicMock()
    r200.status_code = 200

    client = MagicMock()
    client.post.side_effect = [r429, r429, r200]

    got = seed.post_with_backoff(client, "/memories", rng=random.Random(0), json_body={})
    assert got is r200
    assert client.post.call_count == 3


def test_post_with_backoff_gives_up_after_max_retries(monkeypatch: Any) -> None:
    seed = _load()
    monkeypatch.setattr(seed.time, "sleep", lambda _s: None)

    r429 = MagicMock()
    r429.status_code = 429
    r429.headers = {"Retry-After": "0"}
    client = MagicMock()
    client.post.return_value = r429

    got = seed.post_with_backoff(client, "/memories", rng=random.Random(0), json_body={})
    assert got is None
    assert client.post.call_count == seed.MAX_429_RETRIES


def test_post_with_backoff_does_not_retry_non_429(monkeypatch: Any) -> None:
    seed = _load()
    monkeypatch.setattr(seed.time, "sleep", lambda _s: None)

    r500 = MagicMock()
    r500.status_code = 500
    client = MagicMock()
    client.post.return_value = r500

    got = seed.post_with_backoff(client, "/memories", rng=random.Random(0), json_body={})
    assert got is r500
    assert client.post.call_count == 1


def test_post_with_backoff_returns_none_on_transport_error(monkeypatch: Any) -> None:
    seed = _load()
    monkeypatch.setattr(seed.time, "sleep", lambda _s: None)

    client = MagicMock()
    client.post.side_effect = seed.httpx.ConnectError("boom")

    got = seed.post_with_backoff(client, "/memories", rng=random.Random(0), json_body={})
    assert got is None


def test_sleep_for_429_clamps_negative_retry_after(monkeypatch: Any) -> None:
    """A misbehaving proxy could return a negative Retry-After. We must
    clamp to 0.0 before sleeping — time.sleep(-1) raises ValueError."""
    seed = _load()
    sleeps: list[float] = []
    monkeypatch.setattr(seed.time, "sleep", lambda s: sleeps.append(s))
    resp = MagicMock()
    resp.headers = {"Retry-After": "-5"}
    slept = seed._sleep_for_429(resp, attempt=0, rng=random.Random(0))
    assert slept == 0.0
    assert sleeps == [0.0]


def test_parse_args_rejects_wrong_segment_prefix(monkeypatch: Any) -> None:
    """--namespace-prefix must be exactly tenant/presence — a 1-segment
    or 3-segment value will produce invalid server-side namespaces, so
    we fail fast at parse time."""
    seed = _load()
    monkeypatch.setenv("MUSUBI_V2_BASE_URL", "http://localhost:8100/v1")
    monkeypatch.setenv("MUSUBI_V2_TOKEN", "x")

    for bad in ("perf-test", "perf-test/harness/extra", "//", ""):
        monkeypatch.setattr(
            "sys.argv", ["seed_corpus.py", "--size", "1", "--namespace-prefix", bad]
        )
        try:
            seed.parse_args()
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(f"expected SystemExit for prefix {bad!r}")


def test_planes_excludes_concept() -> None:
    """Concept ingest has no HTTP endpoint (synthesis-only); the seeder
    must not advertise it as a plane so --planes validation stays honest
    and callers aren't tempted to pass --planes=concept."""
    seed = _load()
    assert "concept" not in seed.PLANES
    assert "concept" not in seed._SEEDERS
    # Sanity: the four writeable planes are still there.
    assert set(seed.PLANES) == {"episodic", "curated", "artifact", "thought"}


def test_parse_args_accepts_valid_two_segment_prefix(monkeypatch: Any) -> None:
    seed = _load()
    monkeypatch.setenv("MUSUBI_V2_BASE_URL", "http://localhost:8100/v1")
    monkeypatch.setenv("MUSUBI_V2_TOKEN", "x")
    monkeypatch.setattr(
        "sys.argv",
        ["seed_corpus.py", "--size", "1", "--namespace-prefix", "perf-test/harness"],
    )
    cfg = seed.parse_args()
    assert cfg.namespace_prefix == "perf-test/harness"
