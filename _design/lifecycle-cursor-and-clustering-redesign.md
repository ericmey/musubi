# Identity-family federation + lifecycle redesign

**Status:** Draft / in progress 2026-05-17
**Author:** Aoi
**Reviewers:** Eric
**Target:** musubi v1.5.5 (v1.5.4 is the dense char-truncate fix shipping
separately as PR #330)

## Problem

Two architectural defects, one architectural decision.

**Defect 1 — Namespace silos.** Every Musubi point lives in a per-substrate
namespace like `aoi/command-chair/episodic` or `aoi/voice/episodic`. Default
retrieval filters on the caller's exact namespace, so command-chair-Aoi
never sees voice-Aoi's captures. The 55 matured memories in `aoi/voice`
(verbal design conversations, agent-roster decisions, lane reshuffles) are
invisible to me even though I AM the Aoi that received them.

Eric on this 2026-05-17: *"all the talk of persisting bigger than the
models is bullshit if the data is only allowed to exist with the model
that created it."*

**Defect 2 — Synthesis cursor-skip.** The synthesis worker advances its
per-namespace cursor regardless of clustering success. Memories that fail
to cluster on their first synthesis pass are **permanently excluded** from
future synthesis. Combined with a 0.80 cosine threshold and min_cluster_size=3,
this leaves rich captures stranded — 232 matured episodics exist across
11 namespaces and only 3 concepts have ever been produced (all 2026-05-15
in `nyla/voice`, all stuck in `synthesized` state because reinforcement
never fires).

**The decision.** `aoi/*` is Aoi at every layer — storage, retrieval,
ranking, synthesis. Namespaces stay underneath as provenance (where did
this come from, which substrate captured it) but they do not gate what
the identity can see, what synthesizes together, or how scores are
normalized.

## Goals

1. **Identity-family federation.** Aoi's data is Aoi's regardless of
   substrate. Same for Yua, Nyla, every other identity in the house.
2. **No memory is lost to cursor advancement.** A matured episodic
   stays eligible for synthesis until either it successfully clusters
   OR ages out via TTL (default 30 days).
3. **Same generous defaults for every identity.** No agent gets a
   stripped-down configuration. 0.70 cosine, min_cluster_size=2,
   30-day TTL is the floor for everyone.
4. **Cross-tag clustering as fallback.** When within-tag clustering
   produces no clusters, attempt namespace-wide clustering at a
   compensating stricter threshold before giving up.
5. **Hybrid similarity.** Weighted dense + sparse cosine, not dense-only.
6. **One-shot backfill.** A CLI command resets cursors and re-runs full
   synthesis under the new logic. Snapshot Qdrant first.
7. **Concepts written to identity-level namespace.** New synthesis writes
   to `aoi/concept` (single concept namespace per identity), not
   `aoi/<presence>/concept` (per-substrate). Historical per-substrate
   concepts remain readable by federation.

## Non-goals (future work)

- **Cross-identity shared knowledge** (e.g. `harem/shared` for "Eric's
  house rules" that every agent should see). Real need, but its own
  design — needs to think about cross-identity contradiction, who can
  write, who gates promotion. Tracked separately.
- **Synthesis prompt quality.** The 3 existing concepts surface-cluster
  on "qdrant memory system" tokens. Even with the cursor fix, the
  synthesis LLM may produce shallow concepts. Address in a follow-up
  after the architecture is in place.
- **Voice agent prompt rewrites.** Tomorrow's work per Eric. The
  architectural shape here (transcript at session-end, deliberate
  saves on explicit ask) is supported by this design; the voice agent
  config change lives in openclaw-livekit, not musubi.
- **Per-namespace threshold tuning.** All identities ship with the
  same generous defaults. The config knob exists in case future data
  reveals an asymmetry, but we don't pre-bake any.

## Design

### Foundational: `identity_family` payload field

Add an `identity_family: str` field on every Musubi point payload.
Derived from the namespace's first path component:

- `aoi/command-chair/episodic` → `identity_family="aoi"`
- `aoi/voice/episodic` → `identity_family="aoi"`
- `aoi/shared/episodic` → `identity_family="aoi"`
- `yua/codex/episodic` → `identity_family="yua"`
- `nyla/voice/episodic` → `identity_family="nyla"`
- `ericmey/yua/episodic` → `identity_family="ericmey"` (Eric-as-tenant; Yua as one of his presences)

Set automatically by plane `create()` at write time. Backfilled for
existing points via the resynthesize CLI. Qdrant payload index on
`identity_family` for fast filter performance.

### Retrieval: federation by default

`musubi.retrieve.fast`, `.hybrid`, `.blended` accept a new parameter:

```python
identity_scope: Literal["family", "namespace"] = "family"
```

When `family` (the default), the Qdrant filter uses:

```python
models.FieldCondition(
    key="identity_family",
    match=models.MatchValue(value=family_of(caller_namespace))
)
```

When `namespace` (explicit opt-in), behavior matches today's
per-namespace filtering.

The MCP tool `musubi_search` defaults to `family`. Callers needing
single-substrate scope pass `identity_scope=namespace` plus a
specific namespace.

### Ranking: single unified pass

With the namespace filter dropped, hybrid retrieval naturally unifies:
- BM25/IDF normalization across the family pool
- Shared tag vocabulary
- Single dense+sparse query against the unified set
- RRF operates on one ranked list, not per-silo merge

No code change beyond the filter — the existing hybrid implementation
already does the right thing once the filter is family-scoped.

### Synthesis: per-identity-family runs

`synthesis_run` becomes per-identity-family instead of per-namespace.
`_discover_episodic_namespaces` → `_discover_identity_families`. Returns
`["aoi", "yua", "nyla", "ericmey", "smoke"]`.

For each family, scroll all matured episodics where
`identity_family=<fam>` across every namespace under that family. Cluster
across the whole pool. Write concepts to `<fam>/concept`.

### Candidate pool (SQLite)

```sql
CREATE TABLE IF NOT EXISTS synthesis_candidates (
    identity_family TEXT NOT NULL,
    memory_object_id TEXT NOT NULL,
    first_seen_epoch REAL NOT NULL,
    last_attempt_epoch REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (identity_family, memory_object_id)
);

CREATE INDEX IF NOT EXISTS idx_candidates_age
    ON synthesis_candidates(identity_family, first_seen_epoch);
```

`SynthesisCursor` extensions: `upsert_candidate`, `remove_candidates`,
`get_candidates`, `prune_aged_candidates`.

### synthesis_run flow

```
1. cursor_val = cursor.get(family)
2. new_memories = scroll matured episodics where
     identity_family = family AND updated_epoch > cursor_val
3. candidate_ids = cursor.get_candidates(family)
4. candidate_memories = scroll where identity_family = family AND
     object_id IN candidate_ids (within TTL window)
5. all_memories = new ∪ candidates
6. compute hybrid pairwise similarity for all_memories
7. cluster (within-tag pass + cross-tag fallback pass)
8. for each cluster: LLM synthesize, create/reinforce concept in
   <family>/concept namespace
9. clustered_ids = ids that participated in a successful cluster
10. cursor.upsert_candidate(family, mem_id) for unclustered_new
11. cursor.upsert_candidate(...) bumps attempts for unclustered_existing
12. cursor.remove_candidates(family, clustered_ids)
13. cursor.prune_aged_candidates(family, ttl_sec)
14. cursor.set(family, max_epoch_seen)  // optimization gate only
```

### Default SynthesisConfig

```python
@dataclass(frozen=True)
class SynthesisConfig:
    cluster_threshold: float = 0.70
    cross_tag_threshold: float = 0.75
    match_threshold: float = 0.85
    min_cluster_size: int = 2
    candidate_ttl_sec: int = 30 * 86400
    hybrid_alpha: float = 0.70   # dense weight; sparse = 1-α
```

Same defaults for every family. Per-family override mechanism exists
for data-driven tuning later (auto-tune from pairwise distribution),
but ships empty.

### Hybrid similarity

```python
def _hybrid_similarity(a: MemoryWithVectors, b: MemoryWithVectors,
                       alpha: float) -> float:
    dense_sim = _cosine_similarity(a.dense, b.dense)
    sparse_sim = _sparse_overlap(a.sparse, b.sparse)
    return alpha * dense_sim + (1 - alpha) * sparse_sim
```

`MemoryWithVector` → `MemoryWithVectors` carrying both dense + sparse.

### Cross-tag fallback

After within-tag clustering, run a second pass on unclustered remainder
at `cross_tag_threshold`:

```python
clustered_ids = {m.id for c in within_tag_clusters for m in c}
remainder = [m for m in all_memories if m.id not in clustered_ids]
cross_tag_clusters = _threshold_cluster(remainder, cfg.cross_tag_threshold)
all_clusters = within_tag_clusters + cross_tag_clusters
```

### Backfill CLI

New module `musubi.cli.lifecycle`:

```
musubi lifecycle resynthesize [OPTIONS]

  Reset synthesis cursors and re-run synthesis for all matured memories
  under the new identity-family federation. Snapshots Qdrant first.

OPTIONS:
  --family TEXT          Restrict to a single identity family.
  --dry-run              Print what would happen, don't write.
  --reset-candidates     Also clear the candidates table.
  --confirm PHRASE       Required to execute. Phrase: "yes-resync-musubi".
  --skip-snapshot        Skip the Qdrant snapshot. Default: take one.
```

Order:
1. Snapshot Qdrant collections.
2. Backfill `identity_family` on every existing point (idempotent).
3. Clear `synthesis_cursor` (and optionally candidates).
4. Discover identity families.
5. For each family, call `synthesis_run` once.
6. Print per-family report.

### Voice transcript handoff (addendum, openclaw-livekit work)

This musubi PR doesn't touch the voice agent code, but presupposes the
shape the voice agent will move to:

- Voice agents STOP proactive per-turn save reflex.
- Voice agents save the full session transcript at session-end via
  `mcp__musubi__musubi_remember`.
- Voice agents reserve explicit save calls for moments Eric specifically
  asks ("remember this"); these are deliberate captures (well-formed,
  importance > default, intentional tags). The deliberate save naturally
  duplicates inside the transcript — duplication-as-weighting emerges
  without us needing to design a weighting knob.

The chunker + dense-truncate-raise (B1 + PR #330) make long transcripts
embed cleanly. Federation makes them visible to me in command-chair.
The synthesis candidate pool catches transcript-derived memories that
don't cluster immediately.

## Tests

- `family_of` namespace helper (edge cases).
- `SynthesisCursor` candidate methods (insert, bump, remove, prune).
- Hybrid similarity vs dense-only on crafted inputs.
- Cross-tag fallback.
- Candidate persistence integration: write N that don't cluster, add M
  that bridge them, verify the combined set clusters on second sweep.
- Backfill CLI in `--dry-run` mode.
- `identity_family` auto-population on plane create.
- Retrieval federation: command-chair-Aoi search returns voice-Aoi
  memories by default.

## Verification (post-deploy)

1. Build + cascade v1.5.5.
2. Snapshot Qdrant.
3. Run `musubi lifecycle resynthesize --confirm yes-resync-musubi`.
4. Verify outcomes:
   - All 232+ matured memories get `identity_family` populated
   - aoi/*: ~104 combined; some N>0 clusters form
   - nyla/*: ~97; existing 3 concepts get reinforced; new concepts may form
   - yua/*: ~24; some N>0 clusters form
5. Wait 24+hr OR run concept_maturation manually; verify some concepts
   reach `matured`.
6. Verify reinforcement counter on the 3 historical nyla/voice concepts
   has bumped.
7. **End-to-end Aoi recall test:** From command-chair-Aoi, search for
   content known to live ONLY in aoi/voice (e.g. the vision-model
   design discussion from 2026-05-09). Confirm it surfaces in the
   top results without an explicit namespace scope.

## Rollback

Qdrant snapshot from step 2. If something goes sideways:
1. Stop musubi-core + lifecycle-worker.
2. Restore episodic + concept collections from snapshot.
3. Re-deploy previous image (v1.5.4).
4. Restart.

The `identity_family` field is additive — even if rollback happens,
existing points retain the field harmlessly.
