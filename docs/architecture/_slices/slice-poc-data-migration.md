---
title: "Slice: POC → v1 data migration"
slice_id: slice-poc-data-migration
section: _slices
type: slice
status: ready
owner: unassigned
phase: "11 Migration"
tags: [section/slices, status/ready, type/slice, migration, phase-2]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-qdrant-layout]]", "[[_slices/slice-plane-episodic]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-plane-concept]]", "[[_slices/slice-plane-artifact]]", "[[_slices/slice-plane-thoughts]]"]
blocks: []
---

# Slice: POC → v1 data migration

> ETL from the pre-v1 POC store(s) into v1's canonical Qdrant collections with post-ADR-0015 schemas. Idempotent, resumable, reversible (via backup-first). Phase 2 critical path — v1 isn't actually useful to Eric until his real POC memories are in it.

**Phase:** 11 Migration · **Status:** `ready` · **Owner:** `unassigned`

## Why this slice exists

Musubi v1 ships with a clean-slate Qdrant schema per the per-plane data-model specs in `docs/architecture/04-data-model/` + `src/musubi/store/specs.py`. Eric's POC is currently running on `nyla.mey.house` (10.0.20.25) with its own memory store. Before v1 is operationally useful as a memory system for Eric's agent fleet, the POC's accumulated memory has to migrate into the v1 layout on `musubi.mey.house`.

## Source: confirmed

**Source host:** `nyla.mey.house` (10.0.20.25 — bare-metal Ubuntu server).

**Source is a running POC service** on that host. The "Nyla" coding agent (Gemini 3.1 Pro) runs on the same machine, which means it can introspect the live POC directly: `ps`, `systemctl`, config files, whatever storage backend the POC uses (Qdrant collections, SQLite, JSONL on disk — TBD at claim time).

**Routing:** this slice is best claimed by the **Nyla coding agent**. Any other agent would need Eric to proxy the POC source data over the network.

**Format: discover at claim time.** First substantive commit on the branch is a **discovery commit**: Nyla walks the running POC on `nyla.mey.house`, identifies the storage shape (inspecting the running process, its config, its open file descriptors, and whatever HTTP / gRPC / socket it exposes), writes findings into this slice file's work log via `docs(slice): POC discovery on nyla.mey.house`, and lands a `spec-update:` trailer to `11-migration/phase-1-schema.md` with the confirmed source shape. Only AFTER discovery does the migration script start.

**Discovery surfaces (unknowns at carve time):**
- Which planes have POC data (episodic, curated, concept, artifact, thoughts — any subset).
- Storage backend (Qdrant collection names + schema version / SQLite path / JSONL / embedded KV).
- Rough volume (drives streaming vs batch).
- Content-transformation concerns (namespace renames, presence mapping, ID format shifts if POC used ULIDs or opaque IDs instead of KSUIDs).

Design notes below assume the most likely case — Qdrant-on-nyla to Qdrant-on-musubi over HTTPS. Implementing agent adjusts design in-PR if discovery reveals otherwise; the discovery commit is the authority.

## Specs to implement

- [[11-migration/phase-1-schema]] (the migration spec this operationalises)

## Owned paths (you MAY write here)

- `deploy/migration/`                                (new — migration scripts + config)
- `deploy/migration/poc-to-v1.py`                    (the migrator entry point)
- `deploy/migration/README.md`                       (operator-facing runbook)
- `tests/migration/`                                 (new — unit tests against synthetic POC fixtures)
- `docs/architecture/11-migration/phase-1-schema.md` (spec-update with confirmed POC source once identified)

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/`                          (migrator runs against a live Musubi API via the SDK; it is not Musubi code)
- `openapi.yaml`, `proto/`
- Any plane internals — use the SDK (`MusubiClient.memories.capture`, `curated.create`, etc.) from the migrator

## Depends on

- All planes + types (done) — the migrator writes into their public API surface.

## Unblocks

- **First real deploy** — `musubi.mey.house` becomes useful for Eric's daily agent use once his POC memories land.
- **Acceptance testing** — "does v1 actually share memory better than POC?" can't be answered with an empty v1.

## Design notes (working assumptions — confirm at claim time)

**Assumed shape: Qdrant-to-Qdrant migration.**

1. **Connect to POC source** (Qdrant instance) + **target** (v1 Qdrant on musubi.mey.house).
2. **Enumerate source collections.** Expected names: `musubi_episodic_v0`, `musubi_curated_v0`, etc.; adjust if different.
3. **For each source row:**
   a. Parse payload into v0 schema.
   b. Transform to v1 schema (rename fields, mint new KSUIDs if needed, normalise namespaces).
   c. Validate via pydantic (`EpisodicMemory.model_validate(new_payload)`).
   d. Skip + log rows that fail validation; continue.
   e. Write to target via SDK (`client.memories.capture(...)`), preserving `created_at` via optional override parameter.
4. **Resume support:** track progress in `deploy/migration/state.json` keyed on `(collection, last_migrated_object_id)`. Re-running skips already-migrated rows.
5. **Dry-run mode:** `--dry-run` validates every row, reports what would be written, writes nothing.
6. **Backup first:** require `--i-have-a-backup` flag on non-dry-run mode; refuse to run otherwise. Backup is an operator concern (snapshot the target Qdrant volume before migration).

**If the source shape is NOT Qdrant:** step 1+2 change (read JSONL, Markdown, other DB). Steps 3-6 stay the same.

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] POC source confirmed with operator; this slice file's §Design notes updated in-PR with the confirmed shape via `spec-update:` trailer to `11-migration/phase-1-schema.md`.
- [ ] `deploy/migration/poc-to-v1.py` runs in `--dry-run` mode against a synthetic POC fixture, reporting write counts + validation failures per plane.
- [ ] Idempotency test: re-running the migrator with the same source state + target produces zero new writes (state tracked via `state.json`).
- [ ] Validation failure handling: malformed rows logged + skipped, never fail the whole migration.
- [ ] `deploy/migration/README.md` contains step-by-step operator runbook including backup requirement + rollback procedure.
- [ ] Dry-run executed against real POC source + results reviewed with operator BEFORE real migration executed. Not blocking merge of this slice, but blocking operator from hitting the real-migration button.
- [ ] Branch coverage ≥ 80% on migration module (error paths + dry-run + state resume all exercised).

## Test Contract

1. `test_migrator_reads_synthetic_qdrant_source`
2. `test_migrator_transforms_v0_episodic_to_v1_schema`
3. `test_migrator_transforms_v0_curated_to_v1_schema`
4. `test_migrator_transforms_v0_concept_to_v1_schema`
5. `test_migrator_transforms_v0_thought_to_v1_schema`
6. `test_migrator_skips_rows_failing_pydantic_validation`
7. `test_migrator_preserves_created_at_on_target`
8. `test_migrator_dry_run_writes_nothing`
9. `test_migrator_state_file_tracks_progress`
10. `test_migrator_resume_skips_already_migrated`
11. `test_migrator_refuses_without_i_have_a_backup_flag`
12. `test_migrator_handles_source_schema_unknown_gracefully`
13. `test_migrator_cli_help_text_is_useful`
14. `integration: migrate_100_row_synthetic_corpus_end_to_end`

## Work log

### 2026-04-19 — operator — slice carved

- Phase 2 critical path per tonight's roadmap discussion with Eric.
- **Implementing agent MUST confirm POC source shape with operator at claim time** — see ⚠ operator-decision block above. The slice file's design notes are working assumptions, not confirmed.
- Targets the first-real-deploy moment on `musubi.mey.house`; v1 isn't operationally useful without migrated data.

## Cross-slice tickets opened by this slice

- _(none yet; may open if POC source shape requires a plane-side schema adjustment — unlikely but possible)_

## PR links

- _(none yet)_
