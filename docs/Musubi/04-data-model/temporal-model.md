---
title: Temporal Model
section: 04-data-model
tags: [bitemporal, data-model, section/data-model, status/complete, time, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[04-data-model/index]]"
reviewed: false
implements: "docs/Musubi/04-data-model/"
---
# Temporal Model

Time in Musubi has two axes: **when something happened in the world** (event time / validity) and **when Musubi learned about it** (ingestion time / transaction time). Most memory systems collapse these — Musubi keeps them separate where it matters.

This mirrors Zep/Graphiti's bitemporal model ([https://arxiv.org/abs/2501.13956](https://arxiv.org/abs/2501.13956)), adapted to our planes and simplified — we don't store an append-only temporal graph, we store validity windows on the few object types where they matter.

## The four time fields

Every object has (`MusubiObject`, `src/musubi/types/base.py:70-73`):

```python
created_at: datetime                 # when Musubi first recorded this object
created_epoch: float                 # same, as unix epoch (query-friendly)
updated_at: datetime                 # last mutation
updated_epoch: float
```

These are **transaction time** — Musubi's clock, not the world's. Episodic memories add `event_at` (when it happened in the world) and `ingested_at` (when Musubi learned of it) (`src/musubi/types/episodic.py:108-109`).

Every memory object (`MemoryObject`: episodic, curated, concept) also has validity (`src/musubi/types/base.py:154-157`):

```python
valid_from: datetime | None = None   # when this fact became true in the world
valid_until: datetime | None = None  # when it stopped being true (None = still valid)
valid_from_epoch: float | None
valid_until_epoch: float | None
```

These are **event time** — the world's clock, according to the best evidence we have.

## Per-type policy

| Type | Transaction time | Validity window | Notes |
|---|---|---|---|
| `EpisodicMemory` | ✓ | optional | `valid_from` ≈ `created_at` typically; `valid_until` rare (e.g., "I lived in Seattle from 2019 to 2024" as a single memory). |
| `CuratedKnowledge` | ✓ | ✓ | Curated facts often have explicit validity (e.g., "CUDA 13 installation steps, valid from driver 575+"). |
| `SynthesizedConcept` | ✓ | optional | The fields exist; deriving them from the contributing memories' validity overlap is planned, not implemented. |
| `SourceArtifact` | ✓ | — | Not a `MemoryObject`; no validity fields. |
| `Thought` | ✓ | — | Not a `MemoryObject`; no validity fields. Read-state handles "still relevant". |

## Bitemporal queries

The interesting queries:

### "What did we know at time T?"

Find objects where `created_epoch ≤ T` and that had not been superseded at T. This is a historical snapshot — "rebuild what I thought I knew last Tuesday."

**Planned; only partly possible today.** There is no API for it, and the current data cannot always answer it. A row stores `superseded_by` but **no supersession timestamp**, so the row alone cannot say whether the replacement happened before or after T. The evidence that exists, and its limits:

- **Lifecycle events.** A supersession made through `transition()` has a `LifecycleEvent` with `occurred_epoch`, which dates it exactly.
- **Curated supersession by the vault watcher records no event** ([[04-data-model/lifecycle#"No silent mutation" rule]]). For those rows the only hint is the old row's `updated_epoch`, set when it was marked superseded, and any later write to that row overwrites it.

A reliable as-of-T query needs a supersession timestamp on the row, or an event on every supersession path.

### "What is currently true in the world?"

Find objects where (`valid_from IS NULL` or `valid_from ≤ now`) and (`valid_until IS NULL` or `valid_until > now`) and `state IN ("matured", "promoted")`. This is the default-query path. Hybrid retrieval applies the window to episodic and curated rows after hydration (`_within_validity`, `src/musubi/retrieve/hybrid.py:588-594`; `_curated_visible_at`, `src/musubi/planes/curated/plane.py:133-145`).

### "What was true at time T in the world?"

`valid_from ≤ T` and (`valid_until IS NULL` or `valid_until > T`). Used when answering history questions: "what GPU was on the host in March 2026?" There is no as-of-T retrieval parameter yet; planned, not implemented.

## Validity inference

**Planned, not implemented.** Today `valid_from` and `valid_until` are set only when a caller or a vault frontmatter supplies them; nothing infers them. The intended design:

1. If a capture includes a clear temporal phrase ("yesterday", "last week", "on 2026-04-10"), resolve it and set `valid_from`.
2. Otherwise leave `valid_from` unset, which the validity window treats as "valid since recorded".
3. Set `valid_until` only when a supersession is explicit (e.g., "I moved from Seattle to Denver on 2026-03-15" supersedes an earlier "I live in Seattle" memory — the earlier one gets `valid_until = 2026-03-15`).

Inference would run during maturation, never on the capture hot path.

## Clock skew and monotonicity

Musubi runs on one host in v1, so wall-clock skew is a non-issue. But we still enforce:

- `updated_epoch ≥ created_epoch` on every write.
- `valid_from ≤ valid_until` when both set.
- Naive datetimes are rejected everywhere (`ensure_utc`, `src/musubi/types/common.py:129-132`).
- Transition events (LifecycleEvent) record their own `occurred_epoch`.

If Musubi ever runs across hosts, causal ordering would need something like Hybrid Logical Clocks. Not needed for a single host.

## Supersession and validity

The relationship between `superseded_by` (transaction-time lineage) and `valid_until` (event-time) is subtle:

- `superseded_by` is set when a **newer record replaces an older one in our index**.
- `valid_until` is set when the **fact stopped being true in the world**.

These are often the same moment (a new record supersedes because the fact changed), but not always:

- **Correction**: we wrote "GPU is 3090" on 2026-04-10. On 2026-04-15 we learn it was actually a 3080 the whole time. We write a corrected memory, mark the old one `superseded_by` the new one, set `valid_from = 2026-04-10` on the new one (the fact was always true from that date), and leave `valid_until = None`. The old memory has `valid_from = 2026-04-10` and now `valid_until = 2026-04-10` (it was wrong — it had no real validity).

- **Change**: we wrote "GPU is 3080" on 2026-04-10. On 2026-04-15 we swap it for a 4090. We write a new memory with `valid_from = 2026-04-15`. The old memory gets `valid_until = 2026-04-15`. Neither is "superseded" — both describe reality; one is just historical.

So supersession ⇒ "correct me"; validity change ⇒ "the world moved on". Distinguish these in UX copy.

## Time display

Internal storage is always UTC. Display is the viewer's timezone (configured per-presence in their adapter). Never persist local-time strings.

## Test Contract

**Module under test:** validators in `src/musubi/types/common.py` and `src/musubi/types/base.py` (tests in `tests/types/`)

1. `test_created_epoch_matches_created_at`
2. `test_updated_epoch_monotone_non_decreasing`
3. `test_valid_from_before_valid_until_enforced`
4. `test_curated_valid_until_excludes_from_default_query`
5. `test_historical_query_at_time_t_reconstructs_state` (planned)
6. `test_validity_inference_parses_yesterday` (planned)
7. `test_validity_inference_parses_absolute_date` (planned)
8. `test_validity_inference_falls_back_to_created_at` (planned)
9. `test_supersession_sets_valid_until_on_correction` (planned)
10. `test_supersession_leaves_valid_until_on_change` (change case, planned)
11. `test_utc_enforced_on_all_datetime_inputs`

Property tests:

12. `hypothesis: for any legal sequence of writes, updated_epoch is monotone per object_id`
13. `hypothesis: valid_until >= valid_from whenever both are set`
