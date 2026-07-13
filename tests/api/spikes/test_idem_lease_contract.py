"""IDEM lease contract — executable future contract for the in-flight acquire/release primitive.

Yua blocker 1 (2026-07-12T21:48): the earlier lease xfail called `lookup` twice and pretended
the first call "claimed" the key — but `lookup` has no claim semantics, so a correct
acquire/release API could not make that test pass without editing the test. It was coupled to
the wrong surface. This file fixes that: the contract is written against the PROPOSED
`acquire`/`release`/`store` primitive, so building it correctly makes the reds flip green with
NO edit to the test.

Two halves:

  1. REFERENCE PROTOTYPE (`_LeaseCache`, all green).  A minimal in-process implementation of
     the proposed primitive, proving the contract is coherent and satisfiable and giving the
     `src` implementer an exact spec. Covers all eight properties Yua named plus req-3 release
     on error/cancel, plus the real-concurrency "executes exactly once" proof.

  2. REAL-TARGET REDS (`xfail(strict=True)` against `musubi.api.idempotency.IdempotencyCache`).
     Assert the real cache exposes the primitive with the same contract. Fail today (the
     methods do not exist); flip to XPASS→fail the moment `src` implements them to spec.

Identity here is the FULL replay identity — (key, body_hash, route/operation, authorized
principal) — tying req 9 (endpoint in identity) to req 3 (the lease). Synthetic content only.

    uv run pytest tests/api/spikes/test_idem_lease_contract.py -v
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import pytest

from musubi.api.idempotency import IdempotencyCache


# --------------------------------------------------------------------------- #
# Reference prototype of the PROPOSED primitive. This is the spec, not src.
# --------------------------------------------------------------------------- #

@dataclass
class _Lease:
    owner: str
    created_at: float
    done: bool = False
    response: dict | None = None
    status: int | None = None


@dataclass
class _LeaseCache:
    """Reference implementation of the proposed acquire/release/store contract.

    Identity is an opaque hashable (the caller builds it from key+body+route+principal).
    `acquire` is the single atomic gate; `lookup`-then-`store` is replaced so there is no
    window in which two callers both see "miss".
    """

    ttl_s: float = 24 * 3600
    stale_after_s: float = 30.0
    _leases: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self, identity, owner: str, *, now: float | None = None) -> tuple[str, dict | None, int | None]:
        """Atomic. Returns (status, response, status_code):
          - "acquired"  — caller owns the in-flight slot; must store+release.
          - "in_flight" — someone else holds a fresh lease; caller waits/replays/409s.
          - "hit"       — a completed response exists; replay it.
          - "conflict"  — reserved for same-key/different-body at the API layer.
        Owner identity is REQUIRED so release is authenticated (property 6)."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            lease = self._leases.get(identity)
            if lease is None:
                self._leases[identity] = _Lease(owner=owner, created_at=now)
                return "acquired", None, None
            if lease.done:
                return "hit", lease.response, lease.status
            # in-flight; steal only if stale (property 5)
            if now - lease.created_at > self.stale_after_s:
                self._leases[identity] = _Lease(owner=owner, created_at=now)
                return "acquired", None, None
            return "in_flight", None, None

    def store(self, identity, owner: str, *, response_status: int, response_body: dict) -> None:
        with self._lock:
            lease = self._leases.get(identity)
            if lease is None or lease.owner != owner or lease.done:
                raise PermissionError("store by non-owner or after completion")
            lease.done = True
            lease.response = response_body
            lease.status = response_status

    def release(self, identity, owner: str) -> bool:
        """Release an incomplete lease (error/cancel path, property 4). A mismatched owner
        must NOT be able to release someone else's lease (property 6)."""
        with self._lock:
            lease = self._leases.get(identity)
            if lease is None:
                return False
            if lease.owner != owner:
                raise PermissionError("release by non-owner")
            if lease.done:
                return False           # completed leases are kept for replay, not released
            del self._leases[identity]
            return True


IDENT = ("idem-key-1", "bodyhash-abc", "POST /v1/episodic", "eric/claude-code")


# ---- property proofs against the reference (all green) -------------------- #

def test_p1_acquire_returns_acquired() -> None:
    c = _LeaseCache()
    status, _, _ = c.acquire(IDENT, owner="o1")
    assert status == "acquired"


def test_p2_second_acquire_is_in_flight() -> None:
    c = _LeaseCache()
    c.acquire(IDENT, owner="o1")
    status, _, _ = c.acquire(IDENT, owner="o2")
    assert status == "in_flight", "a fresh in-flight lease must block a second acquirer"


def test_p3_owner_store_then_replay_is_hit() -> None:
    c = _LeaseCache()
    c.acquire(IDENT, owner="o1")
    c.store(IDENT, owner="o1", response_status=202, response_body={"object_id": "x"})
    status, body, code = c.acquire(IDENT, owner="o2")
    assert status == "hit" and body == {"object_id": "x"} and code == 202


def test_p4_release_on_error_frees_the_slot() -> None:
    c = _LeaseCache()
    c.acquire(IDENT, owner="o1")
    assert c.release(IDENT, owner="o1") is True          # handler raised → release
    status, _, _ = c.acquire(IDENT, owner="o2")
    assert status == "acquired", "after a release the next caller must be able to acquire"


def test_p5_stale_owner_can_be_stolen() -> None:
    c = _LeaseCache(stale_after_s=0.0)
    c.acquire(IDENT, owner="o1", now=100.0)
    status, _, _ = c.acquire(IDENT, owner="o2", now=100.0 + 1.0)   # past stale window
    assert status == "acquired", "a stale in-flight lease must be reclaimable (no permanent wedge)"


def test_p6_mismatched_owner_cannot_release_or_store() -> None:
    c = _LeaseCache()
    c.acquire(IDENT, owner="o1")
    with pytest.raises(PermissionError):
        c.release(IDENT, owner="o2")
    with pytest.raises(PermissionError):
        c.store(IDENT, owner="o2", response_status=202, response_body={})


def test_p7_bounded_waiter_never_double_executes() -> None:
    """A waiter that gets in_flight must NOT fall through to execute; it polls until hit or a
    bounded deadline, then it is the caller's job to 409/425 — never a second mutation."""
    c = _LeaseCache()
    c.acquire(IDENT, owner="o1")
    executed = []
    deadline = 5          # bounded polls, not unbounded spin
    for _ in range(deadline):
        status, body, _ = c.acquire(IDENT, owner="w")
        if status == "hit":
            break
        if status == "in_flight":
            continue
        executed.append(status)      # "acquired" here would be a double-execution
    assert executed == [], f"waiter double-acquired instead of waiting: {executed}"


def test_p8_real_concurrency_executes_exactly_once() -> None:
    """The point of the whole primitive: N threads racing the same identity → the handler body
    runs ONCE. Deterministic barrier so all threads reach acquire together."""
    c = _LeaseCache()
    n = 12
    barrier = threading.Barrier(n)
    executions = []
    exec_lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()                       # force a real simultaneous race
        status, _, _ = c.acquire(IDENT, owner=f"o{i}")
        if status == "acquired":
            with exec_lock:
                executions.append(i)         # the mutation happens here
            c.store(IDENT, owner=f"o{i}", response_status=202, response_body={"object_id": "x"})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(executions) == 1, f"{n} concurrent callers executed {len(executions)} times, must be exactly 1"


# ---- executable future contracts against the REAL cache (strict xfail) ---- #

@pytest.mark.xfail(strict=True, reason="IDEM lease: IdempotencyCache has no acquire() yet — fix pending")
def test_real_cache_exposes_acquire() -> None:
    c = IdempotencyCache()
    # SECURE CONTRACT: an acquire primitive exists and gates in-flight. Today: AttributeError.
    status, _, _ = c.acquire(IDENT, owner="o1")            # type: ignore[attr-defined]
    assert status == "acquired"
    status2, _, _ = c.acquire(IDENT, owner="o2")           # type: ignore[attr-defined]
    assert status2 in ("in_flight", "conflict"), "second concurrent acquire must not be a free slot"


@pytest.mark.xfail(strict=True, reason="IDEM lease: IdempotencyCache has no release() yet — fix pending")
def test_real_cache_exposes_release() -> None:
    c = IdempotencyCache()
    c.acquire(IDENT, owner="o1")                            # type: ignore[attr-defined]
    freed = c.release(IDENT, owner="o1")                    # type: ignore[attr-defined]
    assert freed is True, "release must free an incomplete lease so the next caller can acquire"


def test_real_cache_lacks_lease_today_control() -> None:
    """TODAY-REALITY control (not xfail): prove the primitive is genuinely absent, so the reds
    above are failing for the RIGHT reason (missing API), not a typo."""
    c = IdempotencyCache()
    assert not hasattr(c, "acquire"), "unexpected: acquire exists — the xfails should XPASS now"
    assert not hasattr(c, "release"), "unexpected: release exists — the xfails should XPASS now"
