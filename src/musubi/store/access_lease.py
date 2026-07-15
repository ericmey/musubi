"""RET-008 / #502 — the shared fenced per-record lease for concurrency-safe ``access_count``.

Qdrant has no atomic increment and its update response reports no matched-count, so a naive
read-modify-write loses updates under real parallelism. A filtered ``set_payload`` IS an atomic
compare-and-swap, and we build a **two-phase, attributable fenced lease** on it. The single
``access_lease_token`` payload field holds ``"<phase>:<issued_us>:<nonce>"`` where phase ∈
{``held``, ``done``}:

1. **Acquire** — write ``held:<issued>:<nonce>`` filtered on the token being EMPTY, or on the
   EXACT observed token when it is EXPIRED (crash takeover — never a blind steal).
2. **Confirm** — read back; proceed only if the stored token is our exact ``held`` token.
3. **Commit (increment + mark done) in ONE fenced update** — set ``access_count = N+1`` AND
   ``token = done:<issued>:<nonce>`` filtered on ``token == our held``. A stale/taken-over holder
   matches zero and cannot write.
4. **Attribute** — read back; our increment landed IFF the stored token is our EXACT ``done``
   token (only our commit could have written it). If it is absent/other, the commit did not land
   (a stall/takeover raced us) → RETRY. We NEVER discard a delivery on a heuristic.
5. **Clear** — write empty filtered on ``token == our done``.

Takeover semantics make crashes safe: an expired ``held`` token means the predecessor crashed
BEFORE committing (its increment did not land) → take it over and increment. An expired ``done``
token means the predecessor committed (its increment DID land) but crashed before clearing → its
increment is already counted, so a taker-over simply proceeds with its own increment on top (and
clears the stale done). Bounded retry + jitter; exhaustion is FAIL-LOUD (raises).

EVERY production writer of ``access_count`` must route through :func:`lease_increment_access` (or
prove it cannot race), and no full-payload write may carry a stale ``access_count`` /
``last_accessed_at`` (use ``memory_serialization`` update-time exclusion) — a bypassing writer
invalidates the mechanism.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

from qdrant_client import QdrantClient, models

from musubi.types.common import utc_now

_LEASE_TTL_US = 5_000_000  # a lease older than this may be taken over (crash/stall recovery)
_MAX_LEASE_ROUNDS = 160  # bounded round budget; caps a pathological live-lock, fail-loud past it
_MAX_BACKOFF_US = 8000  # max jittered backoff between retry rounds (de-synchronizes contenders)


class AccessLeaseExhausted(RuntimeError):
    """Lease contention did not resolve within the bounded round budget — fail loud."""


def _pair_conditions(namespace: str, object_id: str) -> list[models.Condition]:
    return [
        models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
        models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id)),
    ]


def _pair_filter(namespace: str, object_id: str) -> models.Filter:
    return models.Filter(must=_pair_conditions(namespace, object_id))


def _token(phase: str, issued_us: int) -> str:
    return f"{phase}:{issued_us}:{secrets.token_hex(12)}"


def _issued_us(token: str) -> int:
    """Parse the issued-at microseconds from a ``<phase>:<issued_us>:<nonce>`` token."""
    try:
        return int(token.split(":", 2)[1])
    except (ValueError, IndexError):
        return 0  # unparseable → ancient → always takeover-eligible


def _read(
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


def _set_op(
    namespace: str, object_id: str, payload: dict[str, Any], fence: models.Condition
) -> models.SetPayloadOperation:
    return models.SetPayloadOperation(
        set_payload=models.SetPayload(
            payload=payload,
            filter=models.Filter(must=[*_pair_conditions(namespace, object_id), fence]),
        )
    )


async def lease_increment_access(
    client: QdrantClient, collection: str, pairs: set[tuple[str, str]]
) -> None:
    """Increment ``access_count`` exactly once for each (namespace, object_id) under the two-phase
    fenced lease, losing no increment under concurrent writers and never falsely attributing on a
    stall/takeover. Batched per collection; bounded retry + jitter. Raises
    :class:`AccessLeaseExhausted` if contention cannot resolve within the budget."""
    remaining = set(pairs)
    for round_index in range(_MAX_LEASE_ROUNDS):
        if not remaining:
            return
        if round_index:
            await asyncio.sleep(secrets.randbelow(_MAX_BACKOFF_US) / 1_000_000)

        now_us = int(time.time() * 1_000_000)
        states = _read(
            client, collection, remaining, ["access_lease_token", "namespace", "object_id"]
        )
        remaining &= set(states)  # a VANISHED delivered row has no counter to bump — drop it.
        if not remaining:
            return

        # ---- phase 1: acquire (empty, or takeover of the EXACT observed EXPIRED token) ----
        held_tokens: dict[tuple[str, str], str] = {}
        acquire_ops: list[models.SetPayloadOperation] = []
        for key in remaining:
            ns, oid = key
            stored = (states[key].payload or {}).get("access_lease_token")
            if not stored:
                fence: models.Condition = models.IsEmptyCondition(
                    is_empty=models.PayloadField(key="access_lease_token")
                )
            elif now_us - _issued_us(str(stored)) > _LEASE_TTL_US:
                # Expired held → predecessor crashed pre-commit (increment did not land); expired
                # done → predecessor committed (already counted) but crashed pre-clear. Either way
                # take over ONLY this exact token and do our own increment.
                fence = models.FieldCondition(
                    key="access_lease_token", match=models.MatchValue(value=str(stored))
                )
            else:
                continue  # a live lease is held by another writer — retry this row next round.
            token = _token("held", now_us)
            held_tokens[key] = token
            acquire_ops.append(_set_op(ns, oid, {"access_lease_token": token}, fence))
        if acquire_ops:
            client.batch_update_points(collection_name=collection, update_operations=acquire_ops)

        # ---- confirm we hold our exact held token, read the count under it ----
        confirmed = _read(
            client,
            collection,
            set(held_tokens),
            ["access_count", "access_lease_token", "namespace", "object_id"],
        )
        now2_us = int(time.time() * 1_000_000)
        now_str = utc_now().isoformat().replace("+00:00", "Z")
        done_tokens: dict[tuple[str, str], str] = {}
        commit_ops: list[models.SetPayloadOperation] = []
        for key, held in held_tokens.items():
            record = confirmed.get(key)
            if record is None:
                continue
            payload = record.payload or {}
            if payload.get("access_lease_token") != held:
                continue  # lost the acquire — retry.
            ns, oid = key
            done = _token("done", now2_us)
            done_tokens[key] = done
            # phase 3: commit = increment + mark done, fenced on our EXACT held token.
            commit_ops.append(
                _set_op(
                    ns,
                    oid,
                    {
                        "access_count": payload.get("access_count", 0) + 1,
                        "access_lease_token": done,
                        "last_accessed_at": now_str,
                    },
                    models.FieldCondition(
                        key="access_lease_token", match=models.MatchValue(value=held)
                    ),
                )
            )
        if commit_ops:
            client.batch_update_points(collection_name=collection, update_operations=commit_ops)

        # ---- phase 4: ATTRIBUTE — our increment landed IFF our exact done token is stored ----
        attributed = _read(
            client, collection, set(done_tokens), ["access_lease_token", "namespace", "object_id"]
        )
        clear_ops: list[models.SetPayloadOperation] = []
        winners: list[tuple[str, str]] = []
        for key, done in done_tokens.items():
            record = attributed.get(key)
            if record is None or (record.payload or {}).get("access_lease_token") != done:
                continue  # commit did NOT land (stall/takeover raced us) — retry, never discard.
            ns, oid = key
            # phase 5: clear, fenced on our exact done token.
            clear_ops.append(
                _set_op(
                    ns,
                    oid,
                    {"access_lease_token": None},
                    models.FieldCondition(
                        key="access_lease_token", match=models.MatchValue(value=done)
                    ),
                )
            )
            winners.append(key)
        if clear_ops:
            client.batch_update_points(collection_name=collection, update_operations=clear_ops)
        for key in winners:
            remaining.discard(key)  # attributed → this delivery's increment is durably committed.

    if remaining:
        raise AccessLeaseExhausted(
            f"access-count lease unresolved for {len(remaining)} row(s) in {collection} "
            f"after {_MAX_LEASE_ROUNDS} rounds"
        )


__all__ = ["AccessLeaseExhausted", "lease_increment_access"]
