"""DATA-001 / #530 — the attributable owner-token lease for concurrency-safe FULL-OBJECT updates.

RET-008 made access-writer-vs-access-writer concurrency safe and preserved the lease-owned access
fields across full-point upserts. It did NOT close the broader race: every full-object UPDATE path
(dedup-merge reinforce, curated same-id update, patch / supersede / concept-update) reads a whole
object and later writes it back, carrying the read-time snapshot of every field it did not mean to
change — so an unrelated concurrent mutation that lands in the read-to-upsert window is silently
overwritten.

Two Qdrant facts shape the fix (Yua, 2026-07-15):

1. A filtered ``set_payload`` is an atomic CAS, but its response exposes no trustworthy
   matched/modified count, and a readback of ``version == expected + 1`` is **not attributable** —
   two contenders proposing the same next version are indistinguishable. The only sound win signal
   is an **exact, unique, never-reused owner token** read back verbatim.
2. ``update_vectors`` is **not** filter-fenced. It must never run before the writer has uniquely
   proven ownership, or a payload-CAS loser could still overwrite the vector.

So this is a single-row, two-phase **attributable owner lease** on the dedicated
``update_lease_token`` payload field (``"own:<issued_us>:<nonce>"``; distinct from
``access_lease_token`` — different lifecycle, never overloaded):

1. **Acquire** — write ``own:<issued>:<nonce>`` fenced on the row being at the EXACT read
   ``version`` AND the token being empty, or on the EXACT observed EXPIRED token (crash takeover —
   never a blind steal).
2. **Attribute acquire** — read back; proceed only if the stored token is our exact token. This is
   the only win signal; a same-next-version contender fails it.
3. **Publish vectors (proven owner only)** — if the update changes vectors, ``update_vectors`` now,
   inside the held critical section. A loser never reaches here, so it can never overwrite a vector.
4. **Publish payload + bump version + release, in ONE fenced update** — ``set_payload`` of ONLY the
   intended-change fields plus ``version = read_version + 1`` and ``update_lease_token = None``,
   fenced on ``update_lease_token == ours``. Because the write set is narrow, unrelated fields are
   never touched → they compose; a same-field conflict is surfaced as a retry (the loser re-reads
   fresh). This fenced publish is the SINGLE commit point.
5. **Attribute publish** — read back; the update landed iff the token is cleared and the version
   advanced. Else a stall/takeover raced us → retry, never a silent overwrite.

Crash safety: an expired token means the owner died mid-update. Because the fenced publish is the
only commit point, a crash before it leaves the payload/version untouched (the whole update is
abandoned — no partial commit) and the row is taken over on its exact expired token by the next
writer, which re-derives its change from the committed current state. A crash between the vector
publish and the payload publish can leave vectors ahead of a not-yet-committed payload under a held
token; the next owner re-derives vectors from the committed content, converging. Bounded retry +
jitter; exhaustion is FAIL-LOUD (raises :class:`MutationLeaseConflict`).
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from qdrant_client import QdrantClient, models

_LEASE_TTL_US = 5_000_000  # an owner token older than this may be taken over (crash/stall recovery)
_MAX_ROUNDS = 160  # bounded round budget; caps a pathological live-lock, fail-loud past it
_MAX_BACKOFF_US = 8000  # max jittered backoff between retry rounds (de-synchronizes contenders)

#: Fields the mutation lease itself stamps on every publish — a caller's change-set must not carry
#: them (``version`` is derived from the fenced read; the token is owned by this seam).
_SEAM_OWNED = frozenset({"version", "update_lease_token"})


class MutationLeaseConflict(RuntimeError):
    """A full-object update could not acquire/publish within the bounded round budget — fail loud."""


@dataclass(frozen=True)
class MutationPlan:
    """What one full-object UPDATE intends to change, computed from a FRESH read each round.

    ``changes`` is ONLY the payload fields this update mutates — never the whole object, so unrelated
    fields are never written and therefore compose. ``vectors`` is the new named-vector mapping to
    publish (``{DENSE: [...], SPARSE: SparseVector(...)}``) or ``None`` when the update does not
    touch vectors. ``skip`` short-circuits the write (e.g. an idempotent no-op) and returns the
    current payload unchanged.
    """

    changes: dict[str, Any]
    vectors: dict[str, Any] | None = None
    skip: bool = False


def _conditions(namespace: str, object_id: str) -> list[models.Condition]:
    return [
        models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
        models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id)),
    ]


def _token(issued_us: int) -> str:
    return f"own:{issued_us}:{secrets.token_hex(12)}"


def _issued_us(token: str) -> int:
    try:
        return int(token.split(":", 2)[1])
    except (ValueError, IndexError):
        return 0  # unparseable → ancient → always takeover-eligible


def _read(client: QdrantClient, collection: str, namespace: str, object_id: str) -> dict[str, Any]:
    records, _ = client.scroll(
        collection_name=collection,
        scroll_filter=models.Filter(must=_conditions(namespace, object_id)),
        limit=1,
        with_payload=True,
        with_vectors=False,
    )
    return dict(records[0].payload or {}) if records else {}


def owned_update(
    client: QdrantClient,
    collection: str,
    *,
    namespace: str,
    object_id: str,
    point_id: str,
    plan: Callable[[dict[str, Any]], MutationPlan],
) -> dict[str, Any]:
    """Publish an attributable, version-fenced, narrow full-object update to one row.

    ``plan(current_payload)`` is invoked under a FRESH snapshot each round and returns the
    :class:`MutationPlan` (intended-change fields + optional new vectors) for that snapshot — so a
    retry always recomputes against the current state. ``point_id`` is the row's deterministic Qdrant
    point id (each plane derives it from ``object_id``); it addresses the ``update_vectors`` write.
    Returns the published payload (or the current payload if the plan is ``skip`` or the row has
    vanished). Raises :class:`MutationLeaseConflict` on bounded-retry exhaustion.
    """
    for round_index in range(_MAX_ROUNDS):
        if round_index:
            time.sleep(secrets.randbelow(_MAX_BACKOFF_US) / 1_000_000)

        current = _read(client, collection, namespace, object_id)
        if not current:
            return current  # a vanished row has nothing to update — nothing to lose.

        read_version = int(current.get("version", 1))
        stored_token = current.get("update_lease_token")
        now_us = int(time.time() * 1_000_000)

        # ---- phase 1: acquire (empty at the exact read version, or takeover of an EXACT expired) --
        if not stored_token:
            token_fence: models.Condition = models.IsEmptyCondition(
                is_empty=models.PayloadField(key="update_lease_token")
            )
        elif now_us - _issued_us(str(stored_token)) > _LEASE_TTL_US:
            token_fence = models.FieldCondition(
                key="update_lease_token", match=models.MatchValue(value=str(stored_token))
            )
        else:
            continue  # a live owner holds this row — retry next round.

        token = _token(now_us)
        client.set_payload(
            collection_name=collection,
            payload={"update_lease_token": token},
            points=models.Filter(
                must=[
                    *_conditions(namespace, object_id),
                    models.FieldCondition(
                        key="version", match=models.MatchValue(value=read_version)
                    ),
                    token_fence,
                ]
            ),
        )

        # ---- phase 2: attribute the acquire — our EXACT token is the only win signal ----
        held = _read(client, collection, namespace, object_id)
        if held.get("update_lease_token") != token:
            continue  # lost the acquire (foreign winner, or the version moved) — retry.

        # ---- compute the intended change against the CONFIRMED-current row ----
        mutation = plan(held)
        if mutation.skip:
            client.set_payload(
                collection_name=collection,
                payload={"update_lease_token": None},
                points=models.Filter(
                    must=[
                        *_conditions(namespace, object_id),
                        models.FieldCondition(
                            key="update_lease_token", match=models.MatchValue(value=token)
                        ),
                    ]
                ),
            )
            return {**held, "update_lease_token": None}  # reflect the released state, not the hold.
        _reject_seam_fields(mutation.changes)

        # ---- phase 3: proven owner publishes vectors (update_vectors is unfenced; safe ONLY here) -
        if mutation.vectors is not None:
            client.update_vectors(
                collection_name=collection,
                points=[models.PointVectors(id=point_id, vector=mutation.vectors)],
            )

        # ---- phase 4: publish narrow payload + bump version + release, fenced on OUR token ----
        publish = {
            **mutation.changes,
            "version": read_version + 1,
            "update_lease_token": None,
        }
        client.set_payload(
            collection_name=collection,
            payload=publish,
            points=models.Filter(
                must=[
                    *_conditions(namespace, object_id),
                    models.FieldCondition(
                        key="update_lease_token", match=models.MatchValue(value=token)
                    ),
                ]
            ),
        )

        # ---- phase 5: attribute the publish — landed iff token cleared AND version advanced ----
        published = _read(client, collection, namespace, object_id)
        if (
            published.get("update_lease_token") is None
            and int(published.get("version", 0)) == read_version + 1
        ):
            return published
        continue  # a stall/takeover raced the publish — retry, never a silent overwrite.

    raise MutationLeaseConflict(
        f"full-object update for ({namespace!r}, {object_id!r}) in {collection} unresolved "
        f"after {_MAX_ROUNDS} rounds"
    )


def _reject_seam_fields(changes: dict[str, Any]) -> None:
    overlap = _SEAM_OWNED & changes.keys()
    if overlap:
        raise ValueError(
            f"MutationPlan.changes must not carry seam-owned field(s) {sorted(overlap)}; "
            "the mutation lease stamps version + token"
        )


__all__ = ["MutationLeaseConflict", "MutationPlan", "owned_update"]
