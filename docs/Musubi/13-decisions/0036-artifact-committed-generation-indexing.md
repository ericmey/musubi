---
title: "ADR 0036: Committed-Generation Artifact Indexing (C4 / ART-001)"
section: 13-decisions
tags: [adr, artifacts, section/decisions, status/accepted, indexing, concurrency, type/adr]
type: adr
status: accepted
updated: 2026-07-15
up: "[[13-decisions/index]]"
reviewed: false
---
# ADR 0036: Committed-Generation Artifact Indexing (C4 / ART-001)

**Status:** accepted
**Date:** 2026-07-15
**Deciders:** Eric, Aoi (proposal validated 2026-07-14; see Issue #451, spikes #452/#453/#454)

## Context

The production upload route saved blob bytes + artifact metadata but never chunked, embedded, or
indexed. The legacy `ArtifactPlane.index()` also published chunks **unsafely**: chunks carried random
ids with no generation tag, publication was a blind upsert with no delete, and the failure path left a
`failed` head while its already-upserted chunks stayed queryable. Re-indexing from more chunks to fewer
left the **old tail** visible; retries and concurrent publishers produced **mixed generations**.

Three spikes established the evidence (test-only, real Qdrant): **#452** rejected deterministic-ids-alone,
delete-before-upsert, upsert-before-delete, an unfenced generation pointer, last-writer-wins, and unsafe
compensating cleanup. **#453** proved Qdrant conditional-update *counts are untrustworthy*, and that a
unique **generation + owner token + exact head readback** identifies the single winner and blocks ABA.
**#454** proved a whole-collection stage + atomic alias is the only zero-gap *global* snapshot — but is
expensive, couples independent uploads, and no existing contract requires it.

## Decision

**Each artifact head carries a single committed generation. Stage chunks under a never-reused
`(generation, owner_token)`, publish by a `publication_version`-fenced conditional head replace, decide
the winner by exact head readback, and expose only chunks whose `(generation, owner)` equal the
committed head.** Do NOT rebuild the whole chunk collection or swap a global alias per upload.

- **Data contract (additive).** The head gains `committed_generation`, `committed_owner`,
  `index_operation_id`, `publication_version`; each chunk gains `generation`, `owner_token`.
  These six fields are an **authorized additive change to the public API contract**: they surface on
  the `SourceArtifact` and `ArtifactChunk` component schemas, and the committed `openapi.yaml` snapshot
  is regenerated to exact parity to reflect them (additive-only — no field is removed or retyped, so
  the frozen v0.1 surface is not broken; per the "additive changes require an ADR" rule this ADR is
  that authorization).
- **Write path.** `upload → durable indexing intent → chunk → embed → stage → publish → retrieve`.
  Upload persists the blob + head, then admits a durable indexing intent. A lifecycle worker
  (`ArtifactIndexer`) chunks + embeds the canonical blob, stages `(generation, owner)`-tagged chunks
  (invisible), fenced-publishes the head, and readback-confirms.
- **Reuse the lifecycle worker/outbox** by an **additive intent-kind** (`artifact_index`) — a registered
  apply handler dispatched from the existing reconcile/claim/lease/attempts/terminal machinery. NOT a
  new job subsystem.
- **Reads are fail-closed and head-first.** `query`, `query_by_artifact`, `chunks_for` resolve the head
  and expose only the committed `(generation, owner)`; a head with no committed generation (legacy /
  indexing / failed) exposes **zero** chunks.
- **Global search** is head-validated candidates + one bounded retry + an explicit `generation_churn`
  degradation warning — deliberately NOT a globally-linearizable exact-K snapshot (no existing contract
  requires one; requiring it would force the #454 alias design as an explicit future product decision).

## Binding conditions (from validation)

1. **C1** — the head fence and success signal key on the **never-reused generation + owner**, never bare
   version (bare-version equality is ABA-vulnerable, #453).
2. **C2** — **every** read path is head-first + committed-pair filtered; a partial conversion silently
   reintroduces old-tail / failed-chunk leakage. Invariants #1–#4/#8 live in this filter.
3. **C3** — chunks stage invisibly; the single head flip is the sole commit point; loser/GC cleanup is
   generation-scoped and storage-only (never deletes a concurrent attempt's fresh generation).
4. **C4** — implement as an additive intent-kind on the coordinator's apply/confirm/cleanup seam.
5. **C5** — global search stays non-linearizable with an honest `generation_churn` warning; never fake a
   zero-gap snapshot via overfetch (#454 refuted that).

## Consequences

- The eight ART-001 acceptance invariants hold: one committed generation; re-index hides the old tail;
  failure keeps the prior generation visible; a first-ever failure exposes zero chunks; same-artifact
  concurrency yields one winner (proven on real Qdrant); different-artifact independence; idempotent
  retry; head `chunk_count` == visible committed count.
- Legacy generation-less heads/chunks remain deserializable and are simply never exposed (fail-closed);
  they are migrated by re-index from the canonical blob, never guessed into a generation.
- Global search may return a bounded partial + `generation_churn` under concurrent publication churn —
  an accepted, honest degradation.

## Alternatives rejected

Whole-collection stage + atomic alias (#454) — correct for global linearizability but expensive and
unrequired. All six unfenced publication variants (#452). Bare-version OCC without an owner token (#453,
ABA). A new general job subsystem for indexing (the lifecycle outbox already provides durable intents).
