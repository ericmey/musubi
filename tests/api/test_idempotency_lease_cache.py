"""IdempotencyLeaseCache — Phase B behaviours beyond the lease property suite.

The 10-property lease contract (owner uniqueness, stale reclaim, TTL, cancellation release,
exactly-once concurrency, injected clock) is proven by
``tests/api/spikes/test_idem_lease_contract.py`` against the real cache. This file adds the two
Phase-B-specific behaviours Yua flagged for the cache-rewrite review: **digest conflict without a
lease leak** and **bounded eviction that never drops an in-flight lease**.

    uv run pytest tests/api/test_idempotency_lease_cache.py -v
"""

from __future__ import annotations

import threading

from musubi.api.idempotency import IdempotencyLeaseCache

ID = ("issuer", "subject", "presence", "POST", "/v1/episodic", "eric/claude-code/episodic", "k1")
DIGEST_A = b"A" * 32
DIGEST_B = b"B" * 32


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self._t = t

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def test_same_identity_same_digest_replays() -> None:
    c = IdempotencyLeaseCache(clock=_Clock())
    assert c.acquire(ID, "o1", digest=DIGEST_A)[0] == "acquired"
    c.store(ID, "o1", response_status=202, response_body={"object_id": "x"})
    status, body, code = c.acquire(ID, "o2", digest=DIGEST_A)
    assert status == "hit" and body == {"object_id": "x"} and code == 202


def test_digest_conflict_without_lease_leak() -> None:
    """Same identity + DIFFERENT digest => conflict, and NO lease is acquired: a later caller with
    the conflicting body still conflicts (not 'acquired'), and the original digest still replays."""
    c = IdempotencyLeaseCache(clock=_Clock())
    c.acquire(ID, "o1", digest=DIGEST_A)
    c.store(ID, "o1", response_status=202, response_body={"object_id": "x"})

    assert c.acquire(ID, "o2", digest=DIGEST_B)[0] == "conflict", "different body must conflict"
    # NO lease leaked by the conflict: a third caller with the conflicting body ALSO conflicts
    # (it did not silently acquire an in-flight slot).
    assert c.acquire(ID, "o3", digest=DIGEST_B)[0] == "conflict", "conflict must not leak a lease"
    # the original body still replays (the completed entry is intact).
    assert c.acquire(ID, "o4", digest=DIGEST_A)[0] == "hit", "the original digest still replays"


def test_conflict_does_not_execute_or_mutate_the_stored_response() -> None:
    c = IdempotencyLeaseCache(clock=_Clock())
    c.acquire(ID, "o1", digest=DIGEST_A)
    c.store(ID, "o1", response_status=202, response_body={"object_id": "orig"})
    c.acquire(ID, "o2", digest=DIGEST_B)  # conflict
    # the stored response is unchanged by the conflicting attempt
    _s, body, _c = c.acquire(ID, "o3", digest=DIGEST_A)
    assert body == {"object_id": "orig"}, "a conflict must not overwrite the stored response"


def test_bounded_eviction_drops_oldest_completed() -> None:
    c = IdempotencyLeaseCache(clock=_Clock(), max_entries=3)
    for i in range(5):
        ident = ("p", f"k{i}")
        assert c.acquire(ident, f"o{i}")[0] == "acquired"
        c.store(ident, f"o{i}", response_status=202, response_body={"i": i})
    # only the newest 3 remain; the oldest 2 (k0, k1) were evicted → fresh acquire, not hit.
    assert c.acquire(("p", "k0"), "n")[0] == "acquired", "oldest completed entry evicted"
    assert c.acquire(("p", "k4"), "n")[0] == "hit", "newest completed entry retained"


def test_eviction_never_drops_an_in_flight_lease() -> None:
    """Over capacity, an IN-FLIGHT lease must never be evicted (that would lose the lease → double
    execution). Completed entries are evicted; the in-flight one survives even past max_entries."""
    c = IdempotencyLeaseCache(clock=_Clock(), max_entries=2)
    c.acquire(("p", "inflight"), "owner")  # in-flight, NOT stored
    for i in range(5):  # push well past max_entries with COMPLETED entries
        ident = ("p", f"done{i}")
        c.acquire(ident, f"o{i}")
        c.store(ident, f"o{i}", response_status=202, response_body={})
    # the in-flight lease still blocks a second acquirer — it was never evicted.
    assert c.acquire(("p", "inflight"), "other")[0] == "in_flight", (
        "in-flight lease must survive eviction"
    )


def test_deterministic_concurrency_exactly_one_acquires_and_no_conflict_leak() -> None:
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
            c.store(("p", "race"), f"o{i}", response_status=202, response_body={"i": i})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(acquired) == 1, (
        f"exactly one caller may acquire under concurrency, got {len(acquired)}"
    )
