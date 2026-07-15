"""RET-008 / #502 — the shared fenced per-record lease for concurrency-safe ``access_count``.

Qdrant has no atomic increment and its update response reports no matched-count, so a naive
read-modify-write loses updates under real parallelism (multiple workers/processes, a future
async client, or a concurrent cross-process writer). A filtered ``set_payload`` IS an atomic
compare-and-swap, though. We use it to run a **fenced per-record lease**:

1. **Acquire** a record's lease by writing ``access_lease_token = <issued_at_us:nonce>`` filtered
   on the token being EMPTY (fresh acquire) or, for crash recovery, on the token matching the
   EXACT stale value we just observed (takeover of an expired lease — never a blind steal).
2. **Hold check**: read back; proceed only if the stored token is ours AND still fresh.
3. **Increment + release in ONE update** fenced on ``access_lease_token == ours``: set
   ``access_count += 1`` and clear the lease. A stale/taken-over holder's fenced write matches
   zero points, so it can never corrupt the counter after takeover.

A *fresh* lease cannot be taken over (takeover requires expiry), so a fresh holder's fenced
increment is guaranteed to apply — no post-write attribution needed. Bounded retry + jitter
de-synchronizes contenders; exhaustion is FAIL-LOUD (raises).

EVERY production writer of ``access_count`` must route through :func:`lease_increment_access`
(or prove it cannot race) — a writer that bumps the counter while bypassing the lease
invalidates the mechanism.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from qdrant_client import QdrantClient, models

from musubi.types.common import utc_now

#: Lease time-to-live (microseconds). A holder that has not incremented+released within this
#: window is considered crashed/stalled and its EXACT token may be taken over. Far larger than the
#: sub-millisecond critical section (3 synchronous Qdrant ops), so only a real freeze triggers it.
_LEASE_TTL_US = 5_000_000

#: Only proceed to the fenced increment while the lease is comfortably fresh — guarantees the
#: token cannot expire mid-write under any non-crash timing.
_FRESHNESS_MARGIN_US = 4_000_000

#: Bounded round budget. Each round resolves ≥1 contended row (exactly one lease winner), so K
#: concurrent same-row writers need ≈K rounds; the cap bounds a pathological live-lock.
_MAX_LEASE_ROUNDS = 64

#: Max jittered backoff (microseconds) between retry rounds — de-synchronizes contenders.
_MAX_BACKOFF_US = 4000


class AccessLeaseExhausted(RuntimeError):
    """Lease contention did not resolve within the bounded round budget — fail loud."""


def _pair_conditions(namespace: str, object_id: str) -> list[models.Condition]:
    return [
        models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
        models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id)),
    ]


def _pair_filter(namespace: str, object_id: str) -> models.Filter:
    return models.Filter(must=_pair_conditions(namespace, object_id))


def _issued_at_us(token: str) -> int:
    """Parse the issued-at microseconds prefix from an ``issued_at_us:nonce`` token."""
    try:
        return int(token.split(":", 1)[0])
    except (ValueError, IndexError):
        return 0  # unparseable → treat as ancient (always expired / stealable)


def _read_pairs(
    client: QdrantClient, collection: str, pairs: set[tuple[str, str]], fields: list[str]
) -> dict[tuple[str, str], Any]:
    """One batched scroll over the EXACT (namespace, object_id) pairs → {(ns, oid): record}."""
    if not pairs:
        return {}
    records, _ = client.scroll(
        collection_name=collection,
        scroll_filter=models.Filter(should=[_pair_filter(ns, oid) for ns, oid in pairs]),
        limit=len(pairs),
        with_payload=fields,
        with_vectors=False,
    )
    out: dict[tuple[str, str], Any] = {}
    for rec in records:
        payload = rec.payload or {}
        key = (payload.get("namespace"), payload.get("object_id"))
        if key in pairs:
            out[key] = rec
    return out


def lease_increment_access(
    client: QdrantClient, collection: str, pairs: set[tuple[str, str]]
) -> None:
    """Increment ``access_count`` exactly once for each (namespace, object_id) under a fenced lease,
    losing no increment under concurrent writers. Batched per collection (one scroll + one batch
    acquire + one readback + one batch increment/release per round); never N+1. Raises
    :class:`AccessLeaseExhausted` if contention cannot resolve within the bounded budget."""
    remaining = set(pairs)
    for round_index in range(_MAX_LEASE_ROUNDS):
        if not remaining:
            return
        if round_index:
            time.sleep(secrets.randbelow(_MAX_BACKOFF_US) / 1_000_000)

        now_us = int(time.time() * 1_000_000)
        states = _read_pairs(
            client, collection, remaining, ["access_lease_token", "namespace", "object_id"]
        )
        remaining &= set(states)  # a VANISHED delivered row has no counter to bump — drop it.
        if not remaining:
            return

        # ---- acquire (fresh on empty, or takeover of the EXACT observed expired token) ----
        my_tokens: dict[tuple[str, str], str] = {}
        acquire_ops: list[models.SetPayloadOperation] = []
        for key in remaining:
            ns, oid = key
            stored = (states[key].payload or {}).get("access_lease_token")
            if not stored:
                fence: models.Condition = models.IsEmptyCondition(
                    is_empty=models.PayloadField(key="access_lease_token")
                )
            elif now_us - _issued_at_us(str(stored)) > _LEASE_TTL_US:
                # Takeover: replace ONLY this exact stale token (never a blind steal).
                fence = models.FieldCondition(
                    key="access_lease_token", match=models.MatchValue(value=str(stored))
                )
            else:
                continue  # a live lease is held by another writer — retry this row next round.
            token = f"{now_us}:{secrets.token_hex(12)}"
            my_tokens[key] = token
            acquire_ops.append(
                models.SetPayloadOperation(
                    set_payload=models.SetPayload(
                        payload={"access_lease_token": token},
                        filter=models.Filter(must=[*_pair_conditions(ns, oid), fence]),
                    )
                )
            )
        if acquire_ops:
            client.batch_update_points(collection_name=collection, update_operations=acquire_ops)

        # ---- confirm we hold the lease (and it is still fresh), read the count under it ----
        held = _read_pairs(
            client,
            collection,
            set(my_tokens),
            ["access_count", "access_lease_token", "namespace", "object_id"],
        )
        now2_us = int(time.time() * 1_000_000)
        now_str = utc_now().isoformat().replace("+00:00", "Z")
        increment_ops: list[models.SetPayloadOperation] = []
        winners: list[tuple[str, str]] = []
        for key, token in my_tokens.items():
            record = held.get(key)
            if record is None:
                continue
            payload = record.payload or {}
            if payload.get("access_lease_token") != token:
                continue  # lost the acquire to a concurrent winner — retry.
            if now2_us - _issued_at_us(token) > _FRESHNESS_MARGIN_US:
                continue  # stalled since acquire — don't trust this lease; re-acquire next round.
            ns, oid = key
            increment_ops.append(
                models.SetPayloadOperation(
                    set_payload=models.SetPayload(
                        payload={
                            "access_count": payload.get("access_count", 0) + 1,
                            "access_lease_token": None,  # release
                            "last_accessed_at": now_str,
                        },
                        # Fence on OUR token: a stale/taken-over holder matches zero → cannot write.
                        filter=models.Filter(
                            must=[
                                *_pair_conditions(ns, oid),
                                models.FieldCondition(
                                    key="access_lease_token", match=models.MatchValue(value=token)
                                ),
                            ]
                        ),
                    )
                )
            )
            winners.append(key)
        if increment_ops:
            # A fresh lease cannot be taken over, so each fenced increment+release is guaranteed to
            # apply — the winner is done.
            client.batch_update_points(collection_name=collection, update_operations=increment_ops)
            for key in winners:
                remaining.discard(key)

    if remaining:
        raise AccessLeaseExhausted(
            f"access-count lease unresolved for {len(remaining)} row(s) in {collection} "
            f"after {_MAX_LEASE_ROUNDS} rounds"
        )


__all__ = ["AccessLeaseExhausted", "lease_increment_access"]
