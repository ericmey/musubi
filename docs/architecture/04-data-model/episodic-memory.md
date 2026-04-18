---
title: Episodic Memory
section: 04-data-model
tags: [data-model, episodic, schema, section/data-model, status/draft, type/spec]
type: spec
status: draft
updated: 2026-04-17
up: "[[04-data-model/index]]"
reviewed: false
---
# Episodic Memory

Source-first, time-indexed recollection. "At time T, in modality M, between participants P, content C was said / happened."

## Pydantic model

```python
# musubi/types/episodic.py

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from musubi.types.common import KSUID, LifecycleState, ArtifactRef

Modality = Literal["text", "voice-transcript", "tool-call", "system-event"]

class EpisodicMemory(BaseModel):
    object_id: KSUID
    namespace: str                      # e.g., "eric/claude-code/episodic"
    schema_version: int = 1

    # Core content
    content: str = Field(min_length=1, max_length=32_000)
    summary: str | None = Field(default=None, max_length=800)
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(default=5, ge=1, le=10)

    # Temporal
    event_at: datetime                  # UTC; when it actually happened
    event_epoch: float
    ingested_at: datetime
    created_at: datetime
    created_epoch: float
    updated_at: datetime
    updated_epoch: float

    # Lifecycle
    version: int = 1
    state: LifecycleState = "provisional"
    reinforcement_count: int = 0
    last_reinforced_at: datetime | None = None
    last_accessed_at: datetime | None = None
    access_count: int = 0

    # Modality + participants
    modality: Modality
    participants: list[str]             # e.g., ["eric", "claude-code"]
    source_context: str                 # e.g., "Claude Code CLI session 2026-04-17T14:23Z"

    # Relationships
    supersedes: list[KSUID] = Field(default_factory=list)
    superseded_by: KSUID | None = None
    merged_from: list[KSUID] = Field(default_factory=list)
    linked_to_topics: list[str] = Field(default_factory=list)
    supported_by: list[ArtifactRef] = Field(default_factory=list)
    contradicts: list[KSUID] = Field(default_factory=list)
    derived_from: KSUID | None = None

    @model_validator(mode="after")
    def _consistency(self):
        # event_at ≤ ingested_at
        if self.event_at > self.ingested_at:
            raise ValueError("event_at cannot be in the future relative to ingested_at")
        # Lifecycle-valid for this plane
        if self.state not in {"provisional", "matured", "demoted", "archived"}:
            raise ValueError(f"invalid state for episodic: {self.state}")
        # Reinforcement consistency
        if self.reinforcement_count > 0 and self.last_reinforced_at is None:
            raise ValueError("last_reinforced_at required when reinforcement_count > 0")
        return self
```

## Qdrant layout

Collection: `musubi_episodic` (shared across tenants).

**Named vectors:**
- `dense_bge_m3_v1` (1024-d, COSINE) — embedding of `content` (or `summary` if present and content > 2048 tokens).
- `sparse_splade_v1` (sparse) — SPLADE++ sparse embedding of same.

**Payload indexes:**

| Field | Type | Purpose |
|---|---|---|
| `namespace` | KEYWORD | tenant/presence/plane scoping |
| `object_id` | KEYWORD | lookup by id |
| `state` | KEYWORD | filter out demoted/provisional in default reads |
| `modality` | KEYWORD | filter voice vs text etc. |
| `tags` | KEYWORD | tag queries |
| `linked_to_topics` | KEYWORD | topical retrieval |
| `event_epoch` | FLOAT | recency queries |
| `ingested_epoch` | FLOAT | "what did we learn recently" |
| `updated_epoch` | FLOAT | dedup-visibility for recent queries |
| `created_epoch` | FLOAT | audit |
| `importance` | INTEGER | threshold filters |
| `reinforcement_count` | INTEGER | promotion eligibility |
| `access_count` | INTEGER | reflect modes (stale / frequent) |
| `participants` | KEYWORD | who-was-involved queries |

## Storage semantics

- On `create`: always `state = "provisional"`. `version = 1`. `reinforcement_count = 0`.
- On `dedup hit` (semantic similarity ≥ 0.92 to an existing point in same namespace): **update existing**, do not create new. Merge tags (union), update `content` (new text wins — we assume new is more current), bump `reinforcement_count`, update `updated_at` / `updated_epoch` and `last_reinforced_at`. Increment `version`.
- On `maturation` (hourly job): if `state == "provisional"` and `created_epoch < now - 1h`, score importance via LLM, normalize tags, set `state = "matured"`.
- On `demotion` (weekly job or explicit): `state = "demoted"`, `updated_at` bumped, `version++`. Point remains queryable only with explicit `include_demoted=true`.
- On `archival`: `state = "archived"`. Removed from default index behaviors; still in snapshots.

Never deleted except via explicit `DELETE /v1/episodic-memories/{id}` (operator scope only).

## API surface

See [[07-interfaces/canonical-api]]. Relevant endpoints:

- `POST /v1/episodic-memories` — create.
- `GET /v1/episodic-memories/{id}` — fetch.
- `PATCH /v1/episodic-memories/{id}` — tag/importance edits (limited fields, audited).
- `DELETE /v1/episodic-memories/{id}` — operator only; hard delete.
- `POST /v1/episodic-memories/query` — scored retrieval.

The `content` field is capped at 32KB. Long exchanges should be ingested as artifacts and cited via `supported_by`.

## Test contract

**Module under test:** `musubi/planes/episodic/`

Required tests:

1. `test_create_sets_provisional_state`
2. `test_create_enforces_namespace_regex`
3. `test_create_rejects_future_event_at`
4. `test_create_populates_created_and_updated_identically`
5. `test_create_auto_embeds_dense_and_sparse_vectors`
6. `test_create_dedup_hit_updates_existing_instead_of_inserting`
7. `test_create_dedup_hit_merges_tags`
8. `test_create_dedup_hit_bumps_reinforcement_count_and_version`
9. `test_create_dedup_hit_updates_content_with_new_text`
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
27. `test_query_respects_include_demoted_flag`
28. `test_forward_compat_reads_schema_version_0_point`  (POC-compatibility)

Edge cases:

29. `test_content_over_32kb_rejected_with_suggestion_to_use_artifact`
30. `test_concurrent_dedup_race_resolves_to_single_winner`  (two parallel creates with near-identical content; one wins, one reinforces)
31. `test_vector_dimension_mismatch_rejected_with_clear_error`

Performance:

32. `test_perf_create_under_100ms_p95_on_reference_host` (integration test)
33. `test_perf_dedup_query_under_30ms_p95`

Property tests:

34. `hypothesis: idempotency — re-ingesting same content N times produces 1 memory with reinforcement_count == N`
35. `hypothesis: lifecycle monotonicity — state transitions never go backwards (except explicit revive operation)`

## Prior art

- Stanford Generative Agents (memory stream): [https://arxiv.org/abs/2304.03442](https://arxiv.org/abs/2304.03442)
- Zep bitemporal facts: [https://arxiv.org/abs/2501.13956](https://arxiv.org/abs/2501.13956)
- Mem0 extract-consolidate: [https://arxiv.org/abs/2504.19413](https://arxiv.org/abs/2504.19413)

## Open questions (tracked for revision)

- Should `content` be compressed at rest (zstd)? Probably not worth it at our scale; Qdrant payload storage handles it. Revisit at 10M+ points.
- Should we store a small sample of raw transcript even when summarized? For now, no — if you want raw, create an artifact.
