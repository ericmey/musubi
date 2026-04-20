---
title: Relationships
section: 04-data-model
tags: [data-model, lineage, relationships, section/data-model, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[04-data-model/index]]"
reviewed: false
implements: "docs/Musubi/04-data-model/"
---
# Relationships

Lineage and cross-references between objects. The catalog below defines the universe of legal pointers — anything not listed here is not a relationship Musubi represents.

Relationships are stored as `list[KSUID]` or `KSUID | None` fields on the subject object, not as a separate graph table. We're not running a graph database in v1 — see [[13-decisions/0004-no-knowledge-graph-v1]]. Reverse lookups are served by KEYWORD indexes on the relevant payload fields.

## Relationship catalog

| Name | Shape | Cardinality | Subject | Allowed targets | Meaning |
|---|---|---|---|---|---|
| `derived_from` | `KSUID \| None` | 0..1 | `SourceArtifact` | `SourceArtifact` | "This artifact was computed from that one" (e.g., summary → raw). |
| `supersedes` | `list[KSUID]` | 0..N | any | same type, same namespace | "This object replaces those older versions." |
| `superseded_by` | `KSUID \| None` | 0..1 | any | same type, same namespace | Reverse of `supersedes`; set when the object is retired. |
| `merged_from` | `list[KSUID]` | 0..N (≥ 3 for concepts) | `SynthesizedConcept`, `CuratedKnowledge` | `EpisodicMemory`, `SynthesizedConcept`, `CuratedKnowledge` | "This concept was distilled from those memories." |
| `promoted_to` | `KSUID \| None` | 0..1 | `SynthesizedConcept` | `CuratedKnowledge` | Set when a concept is promoted. |
| `promoted_from` | `KSUID \| None` | 0..1 | `CuratedKnowledge` | `SynthesizedConcept` | Reverse. |
| `linked_to_topics` | `list[str]` | 0..N | `CuratedKnowledge`, `SynthesizedConcept` | topic strings (not KSUIDs) | Obsidian-style topical cross-references; parsed from `[[foo]]`. |
| `supported_by` | `list[ArtifactRef]` | 0..N | `CuratedKnowledge`, `SynthesizedConcept`, `EpisodicMemory` | `SourceArtifact` + optional `chunk_id` | Citations. |
| `contradicts` | `list[KSUID]` | 0..N | `SynthesizedConcept`, `CuratedKnowledge` | `SynthesizedConcept`, `CuratedKnowledge` | Symmetric: LLM-detected or human-flagged contradiction. |
| `in_reply_to` | `KSUID \| None` | 0..1 | `Thought` | `Thought` | Threaded conversation link. |

## `ArtifactRef`

`supported_by` uses a richer shape than a bare KSUID because a citation usually needs to resolve to a specific chunk inside a long artifact:

```python
class ArtifactRef(BaseModel):
    artifact_id: KSUID
    chunk_id: KSUID | None = None        # None = the whole artifact
    quote: str | None = None             # verbatim quote; max 1000 chars; used for display
    offset_start: int | None = None
    offset_end: int | None = None
```

The UI layer uses `quote` + offsets to render a snippet; `chunk_id` is authoritative for server-side retrieval.

## Rules

### 1. Same-plane supersession

`supersedes` and `superseded_by` only link objects of the **same type** and **same namespace**. A curated file cannot "supersede" an episodic memory, and vice versa. Enforced at transition time (see [[04-data-model/lifecycle]]).

### 2. No cycles

`supersedes` is a DAG: `A supersedes B` plus `B supersedes A` is rejected at write time. Transition function walks the chain up to a depth cap (10) and rejects if it revisits the subject.

### 3. Merged-from is typed-lax

`merged_from` on a concept is typically episodic, but we allow mixing: a concept can be distilled from episodic memories + older concepts (when re-synthesizing). The `merged_from_planes` field annotates which planes contributed; the scorer uses this later to weight concept provenance.

### 4. Promoted links are bidirectional and immutable

Once a concept is promoted, its `promoted_to` and the curated's `promoted_from` are set atomically and never cleared. Even if the concept is later demoted or the curated is human-edited, the historical link stays — it's lineage, not a live pointer.

### 5. Contradictions are symmetric

`A contradicts B` must coexist with `B contradicts A`. The writer of the contradiction is responsible for writing both sides; the transition function checks symmetry post-write and repairs if asymmetric (logging a warning).

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
musubi_curated:   promoted_from (KEYWORD), supersedes (KEYWORD, array), merged_from (KEYWORD, array)
musubi_concept:   promoted_to (KEYWORD), merged_from (KEYWORD, array), contradicts (KEYWORD, array)
musubi_episodic:  supported_by.artifact_id (KEYWORD, array)
musubi_*:         superseded_by (KEYWORD)
```

See [[04-data-model/qdrant-layout]] for the full index table.

## Graph-shaped questions, without a graph DB

A surprising number of graph-shaped questions can be answered with payload filters + a second query to hydrate referenced objects. The cost: O(hops × query-roundtrips). For Musubi v1, 2-hop is the practical limit; we use it for:

- **"What memories support this curated fact?"** — 1 hop via `supported_by`.
- **"What supersession chain ends at this object?"** — N hops via `superseded_by`; capped at depth 10.
- **"Find every curated that shares a topic with this concept."** — 1 hop via topic filter.

If we need richer traversal later (e.g., "shortest path from this memory to its influenced curated"), we'll revisit graph DBs. See [[13-decisions/0004-no-knowledge-graph-v1]] for the reasoning.

## Test Contract

**Module under test:** `musubi/types/*` validators + `musubi/lifecycle/transitions.py`

1. `test_supersedes_enforces_same_type`
2. `test_supersedes_enforces_same_namespace`
3. `test_supersedes_rejects_cycle`
4. `test_superseded_by_set_atomically_with_supersedes`
5. `test_promoted_to_and_promoted_from_set_atomically`
6. `test_promoted_link_not_clearable_after_demote`
7. `test_merged_from_requires_min_3_for_concept`
8. `test_merged_from_allows_mixed_planes`
9. `test_contradiction_is_symmetric_after_write`
10. `test_asymmetric_contradiction_logged_and_repaired`
11. `test_artifactref_chunk_id_optional`
12. `test_artifactref_quote_length_limited`
13. `test_reverse_lookup_promoted_from_returns_expected_set`
14. `test_reverse_lookup_supported_by_uses_array_index`
15. `test_supersession_chain_walk_depth_capped`
16. `test_linked_to_topics_accepts_unresolved_strings`
17. `test_in_reply_to_chain_walks_correctly`

Property tests:

18. `hypothesis: supersession DAG has no cycles across any sequence of legal writes`
19. `hypothesis: contradictions are symmetric at every quiescent state`
