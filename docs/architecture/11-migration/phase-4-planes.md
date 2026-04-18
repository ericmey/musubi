---
title: "Phase 4: Planes"
section: 11-migration
tags: [collections, migration, phase-4, planes, section/migration, status/stub, type/migration-phase]
type: migration-phase
status: stub
updated: 2026-04-17
up: "[[11-migration/index]]"
prev: "[[11-migration/phase-3-reranker]]"
next: "[[11-migration/phase-5-vault]]"
reviewed: false
---
# Phase 4: Planes

Split the single `musubi_episodic_v2` collection into the five-collection v1 layout: `musubi_episodic`, `musubi_curated`, `musubi_concept`, `musubi_artifact_chunks`, `musubi_thoughts`.

## Goal

Each plane has its own retention rules, payload indexes, and growth profile. The plane-awareness makes retrieval, lifecycle, and scoring cleaner to express.

## Changes

### Create new collections

All five with named vectors matching phase 2. See [[08-deployment/qdrant-config]] for per-collection payload indexes.

### Migrate episodic

`musubi_episodic_v2` is renamed `musubi_episodic`. Functionally no data change; just rename + reconfigure indexes to match the v1 schema.

### Create `musubi_thoughts`

Move the thoughts data out of whatever it's currently in (POC stores thoughts in the same collection with a type flag). Script:

1. Scroll all points where `content_type == "thought"` (or similar marker).
2. Re-embed (if needed — should already be BGE-M3 post phase-2).
3. Upsert into `musubi_thoughts` with the v1 payload schema.
4. Delete from the origin collection.

Runs offline (maintenance window).

### Create `musubi_curated`

Empty at first. Phase 5 populates it.

### Create `musubi_concept`

Empty at first. Phase 6 populates it.

### Create `musubi_artifact_chunks`

Empty at first. Artifact upload API (already stubbed) starts writing here.

### Blended namespace

Introduce `eric/_shared/blended` namespace addressing that fans out to multiple plane collections. See [[05-retrieval/blended]]. Implementation: a retrieve-time expansion, not a physical collection.

### Capture routing

`MemoryCreate.namespace` now has 3-part structure: `{tenant}/{presence}/{plane}`. The write path routes to the matching collection based on the third segment:

- `…/episodic` → `musubi_episodic`.
- `…/curated` → `musubi_curated`.
- `…/artifact` → `musubi_artifact_chunks` (for chunks of an uploaded blob).

Thoughts are routed by endpoint, not namespace (they live in `musubi_thoughts` by default).

## Done signal

- All five collections exist with correct schema.
- Every plane's capture path ends up in the right collection.
- Thoughts collection has matching point count vs old.
- Blended retrieval works against a seed set.

## Rollback

Keep `musubi_episodic_v2` as a read-only fallback for one week. If bugs surface, route reads back to it temporarily.

## Smoke test

```
# Thoughts
> thought_send to livekit-voice: "hello"
> thought_check as livekit-voice: (see the thought)

# Blended retrieve
> retrieve from eric/_shared/blended: "restart livekit"
# Results should span episodic (for past attempts) + curated (for runbook; if any).
```

## Estimate

~2 weeks. Thought migration + blended fanout path are the work.

## Pitfalls

- **Thought payload differences.** POC thoughts have `from`/`to` as plain strings; v1 normalizes to presence names like `eric/claude-code`. Migration must canonicalize.
- **Blended namespace expansion is retrieval-only.** Never materialize a `blended` collection; it's a virtual address.
- **Deleting old collections too early.** Wait at least a week with fallback available.
