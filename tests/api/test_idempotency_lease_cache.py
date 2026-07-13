"""IdempotencyLeaseCache — Phase B behaviours beyond the lease property suite.

The 10-property lease contract (owner uniqueness, no time-based live-lease reclaim, completed-lease
TTL, cancellation release, exactly-once concurrency, injected clock) is proven by
``tests/api/spikes/test_idem_lease_contract.py`` against the real cache. This file adds the
Phase-B-specific behaviours Yua flagged for the cache-rewrite review: digest conflict without a
lease leak, mandatory digest, bounded eviction of the COMPLETED set (which converges even when
acquire traffic stops), and config validation.

    uv run pytest tests/api/test_idempotency_lease_cache.py -v
"""

from __future__ import annotations

import threading

import pytest

from musubi.api.idempotency import CompletedResponse, IdempotencyLeaseCache

ID = ("issuer", "subject", "presence", "POST", "/v1/episodic", "eric/claude-code/episodic", "k1")
DIGEST_A = b"A" * 32
DIGEST_B = b"B" * 32
_D = bytes(32)
_RX = CompletedResponse(status=202, raw_headers=(), body=b"x")
_RORIG = CompletedResponse(status=202, raw_headers=(), body=b"orig")


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self._t = t

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


# --------------------------------------------------------------------------- #
# digest: mandatory, conflict without leak
# --------------------------------------------------------------------------- #


def test_same_identity_same_digest_replays() -> None:
    c = IdempotencyLeaseCache(clock=_Clock())
    assert c.acquire(ID, "o1", digest=DIGEST_A)[0] == "acquired"
    c.store(ID, "o1", response=_RX)
    status, completed = c.acquire(ID, "o2", digest=DIGEST_A)
    assert status == "hit" and completed == _RX


def test_digest_conflict_without_lease_leak() -> None:
    """Same identity + DIFFERENT digest => conflict, and NO lease is acquired: a later caller with
    the conflicting body still conflicts (not 'acquired'), and the original digest still replays."""
    c = IdempotencyLeaseCache(clock=_Clock())
    c.acquire(ID, "o1", digest=DIGEST_A)
    c.store(ID, "o1", response=_RX)
    assert c.acquire(ID, "o2", digest=DIGEST_B)[0] == "conflict", "different body must conflict"
    assert c.acquire(ID, "o3", digest=DIGEST_B)[0] == "conflict", "conflict must not leak a lease"
    assert c.acquire(ID, "o4", digest=DIGEST_A)[0] == "hit", "the original digest still replays"


def test_conflict_does_not_mutate_the_stored_response() -> None:
    c = IdempotencyLeaseCache(clock=_Clock())
    c.acquire(ID, "o1", digest=DIGEST_A)
    c.store(ID, "o1", response=_RORIG)
    c.acquire(ID, "o2", digest=DIGEST_B)  # conflict
    _s, completed = c.acquire(ID, "o3", digest=DIGEST_A)
    assert completed == _RORIG, "a conflict must not overwrite the stored response"


def test_digest_is_mandatory_omission_and_bad_shape_cannot_create_a_lease() -> None:
    c = IdempotencyLeaseCache(clock=_Clock())
    with pytest.raises(TypeError):
        c.acquire(ID, "o1")  # type: ignore[call-arg]  # omitted digest
    with pytest.raises(TypeError):
        c.acquire(ID, "o1", digest=b"too-short")  # wrong length
    with pytest.raises(TypeError):
        c.acquire(ID, "o1", digest="notbytes")  # type: ignore[arg-type]  # wrong type
    # none of the rejected calls created a lease — a valid acquire still gets a fresh slot.
    assert c.acquire(ID, "o1", digest=DIGEST_A)[0] == "acquired", (
        "a rejected digest must not create a lease"
    )


# --------------------------------------------------------------------------- #
# bounded eviction of the COMPLETED set (converges on store, never drops in-flight)
# --------------------------------------------------------------------------- #


def test_bounded_eviction_keeps_newest_completed() -> None:
    c = IdempotencyLeaseCache(clock=_Clock(), max_entries=3)
    for i in range(5):
        ident = ("p", f"k{i}")
        c.acquire(ident, f"o{i}", digest=_D)
        c.store(
            ident,
            f"o{i}",
            response=CompletedResponse(status=202, raw_headers=(), body=str(i).encode()),
        )
    assert c.acquire(("p", "k0"), "n", digest=_D)[0] == "acquired", "oldest completed evicted"
    assert c.acquire(("p", "k1"), "n", digest=_D)[0] == "acquired", (
        "second-oldest completed evicted"
    )
    assert c.acquire(("p", "k4"), "n", digest=_D)[0] == "hit", "newest completed retained"


def test_all_inflight_burst_then_store_converges_to_max() -> None:
    """Yua's required red: an all-in-flight burst then all-store, with NO further acquire traffic,
    must still converge the COMPLETED set to max_entries and keep the newest-by-COMPLETION.

    (The old eviction ran only on acquire, so with traffic stopped the completed set stayed >max
    forever. Eviction now runs on store.)"""
    c = IdempotencyLeaseCache(clock=_Clock(), max_entries=2)
    for i in range(5):  # acquire 5 in-flight FIRST (no store yet)
        assert c.acquire(("p", f"b{i}"), f"o{i}", digest=_D)[0] == "acquired"
    for i in range(
        5
    ):  # then store all — eviction converges the completed set here, no more acquires
        c.store(
            ("p", f"b{i}"),
            f"o{i}",
            response=CompletedResponse(status=202, raw_headers=(), body=str(i).encode()),
        )
    # exactly the 2 newest-by-completion (b3, b4) survive; the rest are gone.
    for i in range(3):
        assert c.acquire(("p", f"b{i}"), "n", digest=_D)[0] == "acquired", f"b{i} must be evicted"
    # re-check the survivors WITHOUT disturbing them: a fresh cache-independent assertion via a peek
    c2 = IdempotencyLeaseCache(clock=_Clock(), max_entries=2)
    for i in range(5):
        c2.acquire(("p", f"b{i}"), f"o{i}", digest=_D)
    for i in range(5):
        c2.store(
            ("p", f"b{i}"),
            f"o{i}",
            response=CompletedResponse(status=202, raw_headers=(), body=str(i).encode()),
        )
    assert c2.acquire(("p", "b3"), "n", digest=_D)[0] == "hit", "b3 (newest-1) must survive"
    assert c2.acquire(("p", "b4"), "n", digest=_D)[0] == "hit", "b4 (newest) must survive"


def test_in_flight_lease_is_never_reclaimed_by_elapsed_time() -> None:
    """B2 (Yua): a LIVE in-flight lease must NEVER be reclaimed on elapsed time. A legitimately slow
    request (owner1) that has not yet completed must keep the key held — a retry (owner2) stays
    in_flight no matter how far the clock advances — because re-executing a slow-but-live request is
    a duplicate mutation, and a process-local cache cannot recover crash state anyway (a crash
    destroys the whole cache). Fail closed on a hung owner beats a double write. The lease is freed
    ONLY by its owner completing (store) or explicitly releasing (error/cancel)."""
    clock = _Clock()
    c = IdempotencyLeaseCache(clock=clock)
    assert c.acquire(ID, "owner-1", digest=DIGEST_A)[0] == "acquired"
    for dt in (30.0, 300.0, 86_400.0, 10_000_000.0):  # arbitrary, well past any old stale window
        clock.advance(dt)
        assert c.acquire(ID, "owner-2", digest=DIGEST_A)[0] == "in_flight", (
            f"a live in-flight lease was reclaimed after {dt}s — never reclaim by time"
        )
    # still freed by the OWNER's explicit exit (cancellation/error path preserved)
    assert c.release(ID, "owner-1") is True
    assert c.acquire(ID, "owner-3", digest=DIGEST_A)[0] == "acquired"


def test_eviction_never_drops_an_in_flight_lease() -> None:
    c = IdempotencyLeaseCache(clock=_Clock(), max_entries=1)
    c.acquire(("p", "inflight"), "owner", digest=_D)  # in-flight, NOT stored
    for i in range(5):  # push many completed entries past the budget
        c.acquire(("p", f"done{i}"), f"o{i}", digest=_D)
        c.store(("p", f"done{i}"), f"o{i}", response=_RX)
    assert c.acquire(("p", "inflight"), "other", digest=_D)[0] == "in_flight", (
        "an in-flight lease must never be evicted"
    )


def test_deterministic_concurrency_exactly_one_acquires() -> None:
    c = IdempotencyLeaseCache(clock=_Clock())
    n = 16
    barrier = threading.Barrier(n)
    acquired: list[int] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        if c.acquire(("p", "race"), f"o{i}", digest=DIGEST_A)[0] == "acquired":
            with lock:
                acquired.append(i)
            c.store(
                ("p", "race"),
                f"o{i}",
                response=CompletedResponse(status=202, raw_headers=(), body=str(i).encode()),
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(acquired) == 1, (
        f"exactly one caller may acquire under concurrency, got {len(acquired)}"
    )


# --------------------------------------------------------------------------- #
# config validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kwargs", [{"max_entries": 0}, {"ttl_s": 0}, {"max_entries": -1}])
def test_pathological_config_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        IdempotencyLeaseCache(clock=_Clock(), **kwargs)  # type: ignore[arg-type]
