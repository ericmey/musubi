# ART-001 Qdrant OCC discriminator

## Pre-encoding observed API behavior

Observed on a disposable digest-pinned Qdrant 1.17.1 server with independent
Python processes and clients:

- two simultaneous conditional `upsert(..., update_filter=version==1,
  update_mode=UPDATE_ONLY, wait=True)` calls both return `completed` operation
  results; exact head readback identifies the winner;
- a no-match conditional update also returns `completed` and exposes no matched
  or modified count;
- a missing-point `UPDATE_ONLY` is a completed no-op, while default UPSERT with
  the same failing filter inserts the missing point;
- a stale retry is a completed no-op after another writer wins;
- reusing version 1 allows the stale writer to publish, so version equality alone
  is vulnerable to ABA; the head filter needs a never-reused version and the
  current owner token;
- forking after constructing a parent Qdrant client crashed both children with
  SIGSEGV. The committed harness launches independent interpreters.

These observations discriminate API behavior only. They do not establish that
Qdrant is the production coordinator.

## Test-only reference seam

The spike stores a deterministic per-artifact head and durable operation records.
Each attempt has a unique generation and owner token. Chunks are staged under both.
Publication conditionally replaces the head only when both expected version and
expected owner token match. Completion is determined by exact head readback, not
the operation result. Cleanup selects only the losing owner/generation. Restart
reconciliation keeps a generation only when the exact head owns it; otherwise it
deletes that operation's staged chunks and records the abort.

This establishes a candidate *per-artifact* read sequence: read head, then filter
chunks by its exact generation and owner. It does not atomically constrain a
namespace-wide vector query across many artifacts. That limitation is an explicit
red, not solved by the head.

## Crash interpretation

- before stage: durable operation, no chunks; restart aborts it;
- after stage: uncommitted chunks; restart deletes only that owner/generation;
- before response / after ambiguous response: the server may have published even
  though the parent received no trustworthy outcome; restart reads the exact head
  and marks the matching operation committed;
- before cleanup: a losing operation leaves staged chunks; restart removes only
  those chunks and cannot remove the winner.

## Migration and production gaps

Generation-less legacy chunks may contain mixed historical indexes. Metadata
`chunk_count` describes neither ownership nor which chunks form one coherent
generation. Count-based assignment is rejected; rebuild from the canonical blob
is required.

Upload-to-index orchestration is absent. A future source slice must explicitly own
invocation, background-job/idempotency semantics, failure visibility, and restart
reconciliation. An `index()`-local generation change cannot close production.

Current immutability/content-address statements also conflict with random chunk IDs
and object-id-addressed blob paths. The data model, docs, blob identity, migration,
and backfill contract require one coordinated reconciliation.

## Decision boundary

Decision pending discriminator. Even a fully passing spike is not an architecture
recommendation, source authorization, merge authorization, or Issue #451 closure.

## Executed evidence

Local arm64, pinned Qdrant 1.17.1:

- normal spike: `11 passed, 7 xfailed`;
- discrimination mode: `--runxfail` reached exactly seven named failures and
  `11 passed`; five wrong candidates executed their bad mutation/query behavior
  against the real server;
- full repository gate: `1725 passed, 197 skipped, 17 deselected, 9 xfailed`,
  coverage `89.46%`;
- teardown audit: zero matching containers, networks, or volumes;
- test-contract coverage: 14/14 accounted for (7 passing controls, 7 strict reds).
