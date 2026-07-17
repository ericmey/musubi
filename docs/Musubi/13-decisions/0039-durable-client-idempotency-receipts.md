---
title: "0039: Durable Client Idempotency Receipts"
section: 13-decisions
tags: [architecture, api, security, idempotency, type/adr, status/accepted]
type: adr
status: accepted
date: 2026-07-17
updated: 2026-07-17
deciders: [Eric, Yua]
---

# 0039: Durable Client Idempotency Receipts

## Context

Musubi's ordinary `Idempotency-Key` replay cache protects a retry while its entry
exists. An external durable outbox has a harder failure seam: Musubi can accept a
POST and return an `object_id`, then the client can die before persisting that
response. Once ordinary replay expires, another POST can create or reinforce a
second mutation. Recent/search/tag probes cannot prove storage absence.

Issue #558 covers the broader server problem of multiple API workers, durable
lease ownership, and orphaned server-operation reconciliation. The external-client
receipt is useful and safely additive without claiming that broader contract.

## Decision

For eligible idempotent writes, `Idempotency-Receipt: durable` opts the request into
a completed-response receipt persisted before a successful response is released to
the client. The explicit opt-in preserves the existing 24h key-reuse semantics for
ordinary idempotent callers. Durable mode requires `Idempotency-Key`. The durable
identity is the existing post-authorization tuple: authenticated issuer, subject,
presence, HTTP method, route operation id, authorized namespace, and idempotency
key. The receipt also stores the byte-exact canonical request digest, exact response
bytes and headers, response SHA-256, and the accepted object id when present.

Eligibility is limited to single-object episodic and curated capture. Batch capture
is rejected before mutation because a list of accepted objects cannot satisfy this
ADR's exact single-object recovery contract.

Add an authenticated v1 lookup endpoint. It authorizes the requested namespace
before accessing receipt storage and accepts the operation, idempotency key, and
request digest needed to reconstruct the same identity. Its result distinguishes:

- `found`: exact identity and digest, with accepted object and response proof;
- `conflict`: identity exists with a different request digest;
- `in_flight`: this process currently owns the request but no completed receipt is
  available;
- `absent`: no authorized receipt exists.

Receipt retention is independent of the ordinary POST replay TTL. Automatic
receipt deletion is deferred until fleet outbox-retention policy can prove that no
client will retry the event; household-scale SQLite growth is preferable to an
unsafe expiry. Receipt content is never returned across principal or namespace
boundaries.

Lookup deliberately requires namespace write authority. The capability belongs to
the principal that could otherwise retry the mutation; namespace read authority
alone does not grant idempotency-ledger visibility. Within that same authorized
identity, `conflict` reveals a different digest for the key and `in_flight` reveals
a live process-local lease. These bounded status disclosures are necessary recovery
signals and never cross the principal/namespace-derived receipt identity.

`WEB_CONCURRENCY=1` remains enforced. A durable completed receipt does not make an
orphaned server-side mutation safely replayable, and this ADR does not claim it
does. Issue #558 remains the owner of that boundary.

The client rule is therefore load-bearing: after any ambiguous response or process
restart, inspect the receipt before re-POSTing. A blind post-restart re-POST can
still create an orphan object before the divergent receipt commit fails; only #558
can close that server-crash interval.

## Consequences

- External drainers can adopt a lost success instead of re-POSTing blindly by
  explicitly requesting durable receipt semantics.
- A receipt-store failure becomes a request failure before success bytes are
  released; Musubi never returns an unreceipted success.
- The observer buffers only the small, already idempotency-eligible write response.
- Receipt lookup is an authorization-sensitive read and must remain behind the
  normal bearer and namespace checks.
- SQLite durability, schema migration, bounded busy handling, and process-restart
  tests become API correctness requirements.

## Alternatives rejected

### Search by event tag

Rejected because retrieval absence does not prove storage absence and ranking or
index degradation can hide a real object.

### Extend ordinary replay TTL indefinitely

Rejected because replay and durable audit have different retention needs, and a
large in-memory cache still disappears on process restart.

### Claim this solves multi-worker idempotency

Rejected. Completed-response durability does not reconcile a server crash between
the underlying mutation and receipt commit. That remains Issue #558.
