---
title: Episodic Memory
section: 04-data-model
tags: [data-model, episodic, schema, section/data-model, status/draft, type/spec]
type: spec
status: draft
updated: 2026-10-01
up: "[[04-data-model/index]]"
reviewed: false
implements: ["src/musubi/cli/validate.py", "src/musubi/planes/episodic/", "src/musubi/store/specs.py", "src/musubi/types/base.py", "src/musubi/types/concept.py", "src/musubi/types/episodic.py", "src/musubi/types/lifecycle_event.py", "src/musubi/types/thought.py", "tests/cli/test_validate.py", "tests/planes/test_episodic.py", "tests/types/"]
---
# Episodic Memory

Source-first, time-indexed recollection. "At time T, in modality M, between participants P, content C was said / happened."

## Pydantic model

The model is `EpisodicMemory` in `src/musubi/types/episodic.py:100-156`. It extends `MemoryObject` (`src/musubi/types/base.py:108-184`), which extends `MusubiObject` (`base.py:30-105`). Read the source for the full field list; the summary below names what is episodic-specific and the defaults that matter.

Inherited (every memory object): `object_id`, `namespace`, `identity_family`, `schema_version`, `created_at`/`created_epoch`, `updated_at`/`updated_epoch`, `version`, `content` (non-empty), `summary`, `tags`, `importance` (1-10, default 5), `reinforcement_count`, `last_accessed_at`, `access_count`, the lineage fields (`supersedes`, `superseded_by`, `merged_from`, `linked_to_topics`, `supported_by`, `contradicts`, `derived_from`) and the validity fields (`valid_from`, `valid_until` and their epochs).

Episodic-specific:

| Field | Type | Default |
|---|---|---|
| `state` | `provisional \| matured \| demoted \| archived \| superseded` | `provisional` |
| `event_at` | `datetime` (UTC) | now; when it happened in the world |
| `ingested_at` | `datetime` (UTC) | now; when Musubi learned about it |
| `modality` | `text \| voice-transcript \| tool-call \| system-event` | `text` |
| `participants` | `list[str]`, e.g. `["admin", "claude-code"]` | `[]` |
| `source_context` | `str`, freeform origin hint | `""` |
| `topics` | `list[str]` | `[]` |
| `importance_last_scored_at` / `importance_last_scored_epoch` | `datetime` / `float` | `None`; set when maturation scores importance |
| `retraction_evidence` | `RetractionEvidence \| None` | `None`; omitted from serialization when unset (see below) |

There is no `event_epoch` or `last_reinforced_at` on episodic memories. The model is `extra="forbid"`, rejects naive datetimes, and requires `retraction_evidence.artifact_namespace` to be the sibling `<tenant>/<presence>/artifact` namespace.

Write-time checks that the model itself does not enforce live in `EpisodicPlane.create` (`src/musubi/planes/episodic/plane.py:219-226`): content over 32 KiB (UTF-8) is refused with a pointer to the artifact plane, and an `event_at` in the future is refused.

## Qdrant layout

Collection: `musubi_episodic` (shared across tenants).

**Named vectors:**
- `dense_bge_m3_v1` (1024-d, COSINE) — embedding of `summary` when present, otherwise `content`.
- `sparse_splade_v1` (sparse) — SPLADE++ sparse embedding of the same text.

**Payload indexes:** the universal set plus the episodic deltas in `src/musubi/store/specs.py:138-173`. See [[04-data-model/qdrant-layout#Payload indexes]] for the table. Fields not in that list (for example `modality`, `participants`, `event_at`) are stored but not indexed.

## Storage semantics

- On `create`: always `state = "provisional"`. `version = 1`. `reinforcement_count = 0`.
- On a dedup candidate (dense cosine similarity ≥ 0.92 to an existing point in the same namespace, `plane.py:70`): merge only if the two are factually compatible — normalized content (NFKC, casefolded, whitespace-collapsed, trailing punctuation stripped) is equal **and** the participant sets are equal (`plane.py:133-152`). A compatible hit updates the existing row instead of inserting: union of tags, `reinforcement_count + 1`, `version + 1`, `updated_at` / `updated_epoch` bumped. Content follows the merge strategy, default `longer-wins` (keep whichever text is strictly longer). An incompatible near-match is inserted as a new row.
- On `maturation` (hourly job): if `state == "provisional"` and `created_epoch < now - 1h`, score importance via LLM, normalize tags, set `state = "matured"`.
- On `demotion` (weekly job or explicit): `state = "demoted"`, `updated_at` bumped, `version++`. Default retrieval returns only `matured` and `promoted` rows; a demoted row comes back only when the caller asks for it (`state_filter`, or `include_archived: true` in `fast` mode; see `src/musubi/api/routers/retrieve.py:119-160`).
- On `archival`: `state = "archived"`. Excluded from default retrieval the same way; still in snapshots.

Deletion is `DELETE /v1/episodic/{id}?namespace=...` (`src/musubi/api/routers/writes_episodic.py:544-600`). It needs write scope on the namespace and is a soft delete by default: a `transition()` to `archived`, which the transition table allows only from `provisional`. `?hard=true` requires operator scope and removes the point from Qdrant entirely, recording a LifecycleEvent.

### Escrow-backed retraction evidence

ADR 0042 adds an optional strict `retraction_evidence` field to the logical
episodic model. Its v1 shape is:

```text
kind: artifact_escrow_v1
artifact_namespace: <derived sibling artifact namespace>
artifact_ref:
  artifact_id: <deterministic escrow KSUID>
original_sha256: <64 lowercase hex>
original_utf8_bytes: <positive integer>
quoted_prefix_utf8_bytes: <positive integer>
omitted_bytes: <non-negative integer>
vector_basis: original
preserved_pointer:
  kind: v2
  live_point: <current immutable content point>
# OR, for a legacy single-point retraction:
preserved_pointer:
  kind: legacy_self
operation_identity_hash: <64 lowercase hex>
request_digest: <64 lowercase hex>
```

`artifact_ref` is whole-artifact only: `chunk_id` and `quote` cannot carry a
value. Prefix bytes plus omitted bytes must equal the recorded original byte
length, and the artifact namespace must be the sibling of the episodic
namespace under the same identity family and presence. The raw idempotency key
is never part of this model.

For a v2 anchor, VAL-002 permits `content`/`summary` projection divergence only
after its existing physical pointer identity and generation checks pass and the
typed evidence proves all of the following from scanned storage:

- the v2 evidence pointer equals the anchor's current `live_point`;
- digest and UTF-8 byte length recomputed from the immutable content point equal
  the evidence;
- the positive-length UTF-8 prefix sliced from that content point occurs
  literally in the committed anchor tombstone, and omitted-byte arithmetic
  reconciles;
- recomputing ADR 0042's deterministic escrow address from this episodic
  namespace, object id, and digest equals the evidence reference; and
- that exact artifact head exists in the fully scanned artifact plane with
  `artifact_state=stored_unindexed` and matching digest and byte length.

The cross-plane conclusion runs only when both episodic and artifact scans
complete. Missing coverage produces `incomplete`/`unknown`, never a clean or
broken inference about an unseen target. `legacy_self` is part of the public
evidence union for the dedicated endpoint, but it cannot waive divergence on a
v2 anchor. Existing inline legacy tombstones without evidence remain valid.
Evidence-absent rows omit `retraction_evidence` during serialization, preserving
the existing HTTP response shape rather than adding a null key; evidence-bearing
rows serialize the complete typed field, and the committed OpenAPI snapshot
declares that optional shape.

## API surface

See [[07-interfaces/canonical-api]]. Relevant endpoints:

- `POST /v1/episodic` — create (202).
- `POST /v1/episodic/batch` — batch create.
- `GET /v1/episodic` — list a namespace, paged.
- `GET /v1/episodic/{id}` — fetch.
- `PATCH /v1/episodic/{id}` — metadata edits; v2 projection replacement is refused.
- `POST /v1/episodic/{id}/retract` — escrow exact original bytes, then commit a
  bounded evidence-bearing tombstone without re-embedding.
- `DELETE /v1/episodic/{id}` — soft archive; `?hard=true` with operator scope removes the point.
- `POST /v1/retrieve` — scored retrieval across eligible planes.

Create content is capped at 32 KiB. The retraction route remains available for
oversized legacy rows because the exact original moves to an unindexed artifact
and only the bounded tombstone remains active. It requires durable idempotency,
both episodic and derived artifact write authorization, and an exact observed
version. The server reserves a 256-byte policy floor for a literal,
complete-grapheme prefix of longer originals; originals at or below that floor
remain quoted in full. Caller prose that would starve the prefix is rejected
rather than truncated.

## Test Contract

**Module under test:** `src/musubi/planes/episodic/`

Behaviour checklist; the implemented tests are in `tests/planes/test_episodic.py` (it lists the bullets it covers).

Required tests:

1. `test_create_sets_provisional_state`
2. `test_create_enforces_namespace_regex`
3. `test_create_rejects_future_event_at`
4. `test_create_populates_created_and_updated_identically`
5. `test_create_auto_embeds_dense_and_sparse_vectors`
6. `test_create_dedup_hit_updates_existing_instead_of_inserting` (compatible hit only)
7. `test_create_dedup_hit_merges_tags`
8. `test_create_dedup_hit_bumps_reinforcement_count_and_version`
9. `test_create_dedup_hit_keeps_longer_content`
10. `test_create_dedup_below_threshold_creates_new`
11. `test_create_dedup_threshold_is_per_plane_configurable`
12. `test_maturation_sets_matured_after_ttl_and_scores_importance`
13. `test_maturation_skips_already_matured`
14. `test_demotion_keeps_record_but_filters_from_default_reads`
15. `test_archival_removes_from_default_queries_but_returns_from_get_by_id`
16. `test_isolation_read_enforcement` (see [[03-system-design/namespaces]])
17. `test_isolation_write_enforcement`
18. `test_access_count_increments_via_batch_update_points`
19. `test_access_count_update_is_not_N_plus_1`
20. `test_patch_importance_creates_lifecycle_event_and_bumps_version`
21. `test_patch_tags_is_additive_by_default`
22. `test_patch_forbids_mutating_content_directly`  (content changes go through deletion + re-create with `supersedes`)
23. `test_delete_requires_operator_scope`
24. `test_delete_creates_audit_event`
25. `test_query_hybrid_returns_scored_results_in_descending_order`
26. `test_query_respects_state_filter_default_excludes_provisional`
27. `test_query_returns_demoted_only_when_requested`

Edge cases:

28. `test_content_over_32kb_rejected_with_suggestion_to_use_artifact`
29. `test_concurrent_dedup_race_resolves_to_single_winner`  (two parallel creates with near-identical content; one wins, one reinforces)
30. `test_vector_dimension_mismatch_rejected_with_clear_error`

Performance:

31. `test_perf_create_under_100ms_p95_on_reference_host` (integration test)
32. `test_perf_dedup_query_under_30ms_p95`

Property tests:

33. `hypothesis: idempotency — re-ingesting same content N times produces 1 memory with reinforcement_count == N`
34. `hypothesis: lifecycle monotonicity — state transitions never go backwards (except explicit revive operation)`

## Prior art

- Stanford Generative Agents (memory stream): [https://arxiv.org/abs/2304.03442](https://arxiv.org/abs/2304.03442)
- Zep bitemporal facts: [https://arxiv.org/abs/2501.13956](https://arxiv.org/abs/2501.13956)
- Mem0 extract-consolidate: [https://arxiv.org/abs/2504.19413](https://arxiv.org/abs/2504.19413)

## Open questions (tracked for revision)

- Should `content` be compressed at rest (zstd)? Probably not worth it at our scale; Qdrant payload storage handles it. Revisit at 10M+ points.
- Should we store a small sample of raw transcript even when summarized? For now, no — if you want raw, create an artifact.
