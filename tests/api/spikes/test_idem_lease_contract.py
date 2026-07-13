"""IDEM lease contract — the SAME property suite run against the reference AND the real cache.

Yua (2026-07-12T21:48 blocker 1, then 22:11 lease gap): the earlier version proved the eight
lease properties only against the reference `_LeaseCache`, while the real-target reds asserted
merely that `acquire`/`release` EXIST and a basic second-status. A partial or broken real
implementation could clear those weak reds. Fixed here.

Design:
  - `_LeaseCache` is the REFERENCE implementation of the acquire/release/store primitive:
    injectable monotonic clock, owner-authenticated store/release, stale-lease reclaim, and TTL
    cleanup of completed leases. It is the spec.
  - Every property test is PARAMETRIZED over two cache factories, both of which MUST pass:
      * `reference`  — the prototype (proves the contract is coherent).
      * `real-cache` — `musubi.api.idempotency.IdempotencyLeaseCache` (Phase B). It now satisfies
        the SAME property suite; these were strict-xfail reds until the real lease landed and are
        green here. (Digest-conflict, mandatory digest, and bounded eviction are covered by
        `tests/api/test_idempotency_lease_cache.py`.)

Identity is the full replay identity — (key, body_hash, route/operation, principal). The clock
is injected (no wall-clock, no `Date.now`) so stale/TTL behaviour is deterministic. Tests/docs
only; no src.

    uv run pytest tests/api/spikes/test_idem_lease_contract.py -v
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from musubi.api.idempotency import IdempotencyCache, IdempotencyLeaseCache

# --------------------------------------------------------------------------- #
# Reference implementation of the PROPOSED primitive (the spec, not src).
# --------------------------------------------------------------------------- #


@dataclass
class _Lease:
    owner: str
    created_at: float
    done: bool = False
    completed_at: float | None = None
    response: dict[str, Any] | None = None
    status: int | None = None


class _LeaseCache:
    """Reference acquire/release/store lease.

    - `clock`: injected monotonic source (seconds, float). No wall-clock.
    - `stale_after_s`: an in-flight lease older than this is reclaimable (crash recovery).
    - `ttl_s`: a COMPLETED lease is replayable until this long after completion, then cleaned.
    """

    def __init__(
        self, *, clock: Callable[[], float], stale_after_s: float = 30.0, ttl_s: float = 24 * 3600
    ) -> None:
        self._clock = clock
        self._stale = stale_after_s
        self._ttl = ttl_s
        self._leases: dict[object, _Lease] = {}
        self._lock = threading.Lock()

    def acquire(
        self, identity: object, owner: str, *, digest: bytes
    ) -> tuple[str, dict[str, Any] | None, int | None]:
        now = self._clock()
        with self._lock:
            self._cleanup_locked(now)
            lease = self._leases.get(identity)
            if lease is None:
                self._leases[identity] = _Lease(owner=owner, created_at=now)
                return "acquired", None, None
            if lease.done:
                return "hit", lease.response, lease.status
            if now - lease.created_at > self._stale:  # reclaim a crashed owner
                self._leases[identity] = _Lease(owner=owner, created_at=now)
                return "acquired", None, None
            return "in_flight", None, None

    def store(
        self, identity: object, owner: str, *, response_status: int, response_body: dict[str, Any]
    ) -> None:
        with self._lock:
            lease = self._leases.get(identity)
            if lease is None or lease.owner != owner or lease.done:
                raise PermissionError("store by non-owner or after completion")
            lease.done = True
            lease.completed_at = self._clock()
            lease.response = response_body
            lease.status = response_status

    def release(self, identity: object, owner: str) -> bool:
        with self._lock:
            lease = self._leases.get(identity)
            if lease is None:
                return False
            if lease.owner != owner:
                raise PermissionError("release by non-owner")
            if lease.done:
                return False  # completed leases are kept for replay
            del self._leases[identity]
            return True

    def cleanup(self) -> None:
        with self._lock:
            self._cleanup_locked(self._clock())

    def _cleanup_locked(self, now: float) -> None:
        expired = [
            k
            for k, v in self._leases.items()
            if v.done and v.completed_at is not None and now - v.completed_at > self._ttl
        ]
        for k in expired:
            del self._leases[k]


IDENT = ("idem-key-1", "bodyhash-abc", "POST /v1/episodic", "eric/claude-code")
_D = bytes(32)  # a fixed SHA-256-length digest (mandatory on acquire)


class _Clock:
    """Deterministic injectable monotonic clock — advance() moves time forward."""

    def __init__(self, t: float = 1000.0) -> None:
        self._t = t

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


# --------------------------------------------------------------------------- #
# Cache factories. Same property suite runs against BOTH.
# --------------------------------------------------------------------------- #


def _make_reference(clock: _Clock) -> _LeaseCache:
    return _LeaseCache(clock=clock, stale_after_s=30.0, ttl_s=100.0)


def _make_real(clock: _Clock) -> IdempotencyLeaseCache:
    # The real target now satisfies the SAME lease contract (Phase B).
    return IdempotencyLeaseCache(clock=clock, stale_after_s=30.0, ttl_s=100.0)


CACHES = [
    pytest.param(_make_reference, id="reference"),
    pytest.param(_make_real, id="real-cache"),
]


# --------------------------------------------------------------------------- #
# The property suite — parametrized over reference + real.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("make_cache", CACHES)
def test_p1_acquire_returns_acquired(make_cache: Callable[[_Clock], Any]) -> None:
    c = make_cache(_Clock())
    assert c.acquire(IDENT, owner="o1", digest=_D)[0] == "acquired"


@pytest.mark.parametrize("make_cache", CACHES)
def test_p2_second_acquire_is_in_flight(make_cache: Callable[[_Clock], Any]) -> None:
    c = make_cache(_Clock())
    c.acquire(IDENT, owner="o1", digest=_D)
    assert c.acquire(IDENT, owner="o2", digest=_D)[0] == "in_flight"


@pytest.mark.parametrize("make_cache", CACHES)
def test_p3_owner_store_then_replay_is_hit(make_cache: Callable[[_Clock], Any]) -> None:
    c = make_cache(_Clock())
    c.acquire(IDENT, owner="o1", digest=_D)
    c.store(IDENT, owner="o1", response_status=202, response_body={"object_id": "x"})
    status, body, code = c.acquire(IDENT, owner="o2", digest=_D)
    assert status == "hit" and body == {"object_id": "x"} and code == 202


@pytest.mark.parametrize("make_cache", CACHES)
def test_p4_owner_only_store_and_release(make_cache: Callable[[_Clock], Any]) -> None:
    c = make_cache(_Clock())
    c.acquire(IDENT, owner="o1", digest=_D)
    with pytest.raises(PermissionError):
        c.release(IDENT, owner="o2")
    with pytest.raises(PermissionError):
        c.store(IDENT, owner="o2", response_status=202, response_body={})


@pytest.mark.parametrize("make_cache", CACHES)
def test_p5_release_after_error_frees_slot(make_cache: Callable[[_Clock], Any]) -> None:
    c = make_cache(_Clock())
    c.acquire(IDENT, owner="o1", digest=_D)
    assert c.release(IDENT, owner="o1") is True  # handler raised/cancelled → release
    assert c.acquire(IDENT, owner="o2", digest=_D)[0] == "acquired"


@pytest.mark.parametrize("make_cache", CACHES)
def test_p6_stale_inflight_is_reclaimable(make_cache: Callable[[_Clock], Any]) -> None:
    clock = _Clock()
    c = make_cache(clock)
    c.acquire(IDENT, owner="o1", digest=_D)
    clock.advance(31.0)  # past stale window (30s)
    assert c.acquire(IDENT, owner="o2", digest=_D)[0] == "acquired", (
        "a crashed owner must not wedge the key"
    )


@pytest.mark.parametrize("make_cache", CACHES)
def test_p7_completed_lease_expires_after_ttl(make_cache: Callable[[_Clock], Any]) -> None:
    clock = _Clock()
    c = make_cache(clock)
    c.acquire(IDENT, owner="o1", digest=_D)
    c.store(IDENT, owner="o1", response_status=202, response_body={"object_id": "x"})
    assert c.acquire(IDENT, owner="o2", digest=_D)[0] == "hit"  # replayable within TTL
    clock.advance(101.0)  # past ttl (100s)
    assert c.acquire(IDENT, owner="o3", digest=_D)[0] == "acquired", (
        "a completed lease must be cleaned after TTL"
    )


@pytest.mark.parametrize("make_cache", CACHES)
def test_p8_bounded_waiter_never_double_executes(make_cache: Callable[[_Clock], Any]) -> None:
    c = make_cache(_Clock())
    c.acquire(IDENT, owner="o1", digest=_D)
    executed = []
    for _ in range(5):  # bounded polls
        status, _, _ = c.acquire(IDENT, owner="w", digest=_D)
        if status == "hit":
            break
        if status == "in_flight":
            continue
        executed.append(status)  # "acquired" here == double execution
    assert executed == [], f"waiter double-acquired instead of waiting: {executed}"


@pytest.mark.parametrize("make_cache", CACHES)
def test_p9_real_concurrency_executes_exactly_once(make_cache: Callable[[_Clock], Any]) -> None:
    c = make_cache(_Clock())
    n = 12
    barrier = threading.Barrier(n)
    executions = []
    errors = []
    exec_lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()  # force a simultaneous race
        try:
            if c.acquire(IDENT, owner=f"o{i}", digest=_D)[0] == "acquired":
                with exec_lock:
                    executions.append(i)
                c.store(IDENT, owner=f"o{i}", response_status=202, response_body={"object_id": "x"})
        except Exception as exc:  # missing/partial primitive: record, don't leak
            with exec_lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"lease primitive raised under concurrency: {errors[0]}"
    assert len(executions) == 1, (
        f"{n} concurrent callers executed {len(executions)} times, must be exactly 1"
    )


@pytest.mark.parametrize("make_cache", CACHES)
def test_p10_clock_is_injected_not_wallclock(make_cache: Callable[[_Clock], Any]) -> None:
    """Stale/TTL must key off the INJECTED monotonic clock, so behaviour is deterministic and
    testable. Proven by holding the clock still: a fresh in-flight lease is NOT reclaimed when
    zero injected time has passed, regardless of real wall-clock elapsed."""
    clock = _Clock()
    c = make_cache(clock)
    c.acquire(IDENT, owner="o1", digest=_D)
    # clock does NOT advance → the lease is not stale → second acquire is blocked
    assert c.acquire(IDENT, owner="o2", digest=_D)[0] == "in_flight", (
        "stale check must use the injected clock"
    )


# --------------------------------------------------------------------------- #
# Real-cache control (not xfail): prove the primitive is genuinely absent, so the reds above
# fail for the RIGHT reason.
# --------------------------------------------------------------------------- #


def test_lease_primitive_lives_in_lease_cache_not_the_deprecated_one() -> None:
    # Phase B: the lease primitive is on IdempotencyLeaseCache (the parametrized suite above runs
    # the full property contract against it). The legacy IdempotencyCache — used only by the
    # pre-auth middleware being removed — deliberately does NOT carry the lease.
    lease = IdempotencyLeaseCache()
    assert hasattr(lease, "acquire") and hasattr(lease, "release") and hasattr(lease, "store")
    legacy = IdempotencyCache()
    assert not hasattr(legacy, "acquire"), "the deprecated cache must not grow the lease primitive"
