---
title: Relationships
section: 04-data-model
tags: [data-model, lineage, relationships, section/data-model, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[04-data-model/index]]"
reviewed: false
implements: "docs/Musubi/04-data-model/"
---
# Relationships

Lineage and cross-references between objects. The catalog below defines the universe of legal pointers — anything not listed here is not a relationship Musubi represents.

Relationships are stored as `list[KSUID]` or `KSUID | None` fields on the subject object (the lineage block of `MemoryObject`, `src/musubi/types/base.py:144-151`, plus the type-specific fields named below), not as a separate graph table. We're not running a graph database in v1 — see [[13-decisions/0004-no-knowledge-graph-v1]]. Reverse lookups are served by KEYWORD indexes on the relevant payload fields.

## Relationship catalog

| Name | Shape | Cardinality | Subject | Allowed targets | Meaning |
|---|---|---|---|---|---|
| `derived_from` | `KSUID \| None` | 0..1 | `SourceArtifact` | `SourceArtifact` | "This artifact was computed from that one" (e.g., summary → raw). |
| `supersedes` | `list[KSUID]` | 0..N | any | same type, same namespace | "This object replaces those older versions." |
| `superseded_by` | `KSUID \| None` | 0..1 | any | same type, same namespace | Reverse of `supersedes`; set when the object is retired. |
| `merged_from` | `list[KSUID]` | 0..N (≥ 3 for concepts, checked by the concept plane) | any `MemoryObject`; set in practice on `SynthesizedConcept` | `EpisodicMemory`, `SynthesizedConcept`, `CuratedKnowledge` | "This concept was distilled from those memories." |
| `promoted_to` | `KSUID \| None` | 0..1 | `SynthesizedConcept` | `CuratedKnowledge` | Set when a concept is promoted. |
| `promoted_from` | `KSUID \| None` | 0..1 | `CuratedKnowledge` | `SynthesizedConcept` | Reverse. |
| `linked_to_topics` | `list[str]` | 0..N | any `MemoryObject` | topic strings (not KSUIDs) | Obsidian-style topical cross-references; parsed from `[[foo]]`. |
| `supported_by` | `list[ArtifactRef]` | 0..N | any `MemoryObject` | `SourceArtifact` + optional `chunk_id` | Citations. |
| `contradicts` | `list[KSUID]` | 0..N | any `MemoryObject`; written today by synthesis on `SynthesizedConcept` | `SynthesizedConcept` | LLM-detected contradiction; synthesis writes both sides. |
| `in_reply_to` | `KSUID \| None` | 0..1 | `Thought` | `Thought` | Threaded conversation link. |
| `supersedes` (thought) | `list[KSUID]` | 0..N | `Thought` | `Thought` | A correction to an earlier thought. |

## `ArtifactRef`

`supported_by` uses a richer shape than a bare KSUID because a citation usually needs to resolve to a specific chunk inside a long artifact:

```python
# src/musubi/types/common.py:140-150
class ArtifactRef(BaseModel):
    artifact_id: KSUID
    chunk_id: KSUID | None = None        # None = the whole artifact
    quote: str | None = None             # verbatim quote, used for display
```

The model is frozen and forbids extra keys; there are no offset fields. `chunk_id` is authoritative for server-side retrieval. The 1000-character cap on `quote` is enforced only on vault frontmatter (`ArtifactRefFrontmatter`, `src/musubi/vault/frontmatter.py:18-23`), not on the API model.

## Rules

### 1. Same-plane supersession

`supersedes` and `superseded_by` only link objects of the **same type** and **same namespace**. A curated file cannot "supersede" an episodic memory, and vice versa. Enforced at transition time: each target must resolve to exactly one row in the subject's collection and namespace, or the transition fails with `invariant_violation` (`src/musubi/lifecycle/transitions.py:262-295`). `merged_from` is deliberately not checked this way; it crosses types by design.

### 2. No cycles

`supersedes` is a DAG: `A supersedes B` plus `B supersedes A` is rejected at write time. The transition function walks the `superseded_by` chain (bounded to 64 hops) and rejects with `circular_supersession` if it reaches the subject (`transitions.py:581-630`).

### 3. Merged-from is typed-lax

`merged_from` on a concept is typically episodic, but we allow mixing: a concept can be distilled from episodic memories + older concepts (when re-synthesizing). A `merged_from_planes` payload index is declared on `musubi_concept` (`src/musubi/store/specs.py:191`), but no model field writes it; weighting concept provenance by source plane is planned, not implemented.

### 4. Promoted links are bidirectional and immutable

Once a concept is promoted, its `promoted_to` and the curated's `promoted_from` are set atomically and never cleared. Even if the concept is later demoted or the curated is human-edited, the historical link stays — it's lineage, not a live pointer.

### 5. Contradictions are symmetric

`A contradicts B` should coexist with `B contradicts A`. The writer is responsible for both sides: synthesis calls `ConceptPlane.add_contradiction` for each direction (`src/musubi/lifecycle/synthesis.py:810-819`). Nothing checks or repairs an asymmetric pair after the fact.

### 6. Linked-to-topics is a string, not a KSUID

Obsidian wikilinks are by file-title/topic, not by ID. A topic may resolve to zero, one, or many files — that's fine; we just store the topic strings. Resolution happens at query time.

## Reverse lookups

Because relationships are stored on the subject, reverse queries go through Qdrant payload filters:

| Question | Query |
|---|---|
| "What curated docs were promoted from this concept?" | `musubi_curated` filter `promoted_from == <concept-id>` |
| "What artifacts cite this one?" | `musubi_*` filters with `supported_by.artifact_id == <id>` (requires ARRAY KEYWORD index) |
| "What's superseded by X?" | `musubi_*` filter `superseded_by == X` |
| "What concepts merged this memory?" | `musubi_concept` filter `merged_from CONTAINS <memory-id>` |

Indexes required (in addition to the per-object standard set):

```
musubi_curated:   promoted_from, supersedes (array), superseded_by
musubi_concept:   promoted_to, merged_from (array), merged_from_planes (array), contradicts (array)
musubi_episodic:  supported_by.artifact_id (array), superseded_by, merged_into
musubi_thought:   in_reply_to
```

All are KEYWORD (`src/musubi/store/specs.py:162-221`). `merged_from` is indexed on `musubi_concept` only, and `superseded_by` on episodic and curated only; a reverse lookup on an unindexed field still works but scans.

See [[04-data-model/qdrant-layout]] for the full index table.

## Graph-shaped questions, without a graph DB

A surprising number of graph-shaped questions can be answered with payload filters + a second query to hydrate referenced objects. The cost: O(hops × query-roundtrips). For Musubi v1, 2-hop is the practical limit; we use it for:

- **"What memories support this curated fact?"** — 1 hop via `supported_by`.
- **"What supersession chain ends at this object?"** — N hops via `superseded_by`; the transition-time cycle walk caps at 64.
- **"Find every curated that shares a topic with this concept."** — 1 hop via topic filter.

If we need richer traversal later (e.g., "shortest path from this memory to its influenced curated"), we'll revisit graph DBs. See [[13-decisions/0004-no-knowledge-graph-v1]] for the reasoning.

## Test Contract

**Module under test:** `src/musubi/types/*` validators + `src/musubi/lifecycle/transitions.py`

Behaviour checklist; implemented tests live in `tests/types/` and `tests/lifecycle/test_lifecycle.py` under their own names.

1. `test_supersedes_enforces_same_type`
2. `test_supersedes_enforces_same_namespace`
3. `test_supersedes_rejects_cycle`
4. `test_superseded_by_set_atomically_with_supersedes`
5. `test_promoted_to_and_promoted_from_set_atomically`
6. `test_promoted_link_not_clearable_after_demote`
7. `test_merged_from_requires_min_3_for_concept`
8. `test_merged_from_allows_mixed_planes`
9. `test_contradiction_is_symmetric_after_write`
10. `test_synthesis_writes_both_contradiction_sides`
11. `test_artifactref_chunk_id_optional`
12. `test_artifactref_frontmatter_quote_length_limited` (vault frontmatter only)
13. `test_reverse_lookup_promoted_from_returns_expected_set`
14. `test_reverse_lookup_supported_by_uses_array_index`
15. `test_supersession_chain_walk_bounded`
16. `test_linked_to_topics_accepts_unresolved_strings`
17. `test_in_reply_to_chain_walks_correctly`

Property tests:

18. `hypothesis: supersession DAG has no cycles across any sequence of legal writes`
19. `hypothesis: contradictions are symmetric at every quiescent state`
