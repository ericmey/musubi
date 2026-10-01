---
title: Object Hierarchy
section: 04-data-model
tags: [data-model, schema, section/data-model, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[04-data-model/index]]"
reviewed: false
implements: "docs/Musubi/04-data-model/"
---
# Object Hierarchy

The object type hierarchy in Musubi. Shown as Python-adjacent pseudocode; the real pydantic models are in `src/musubi/types/` and are the authority (`base.py`, `episodic.py`, `curated.py`, `concept.py`, `artifact.py`, `thought.py`).

## Base

```python
class MusubiObject:                     # src/musubi/types/base.py:30-105
    object_id: KSUID = <minted>          # generated at construction when omitted
    namespace: str                       # tenant/presence/plane; required
    identity_family: str = <derived>     # first namespace segment when omitted
    schema_version: int = 1
    created_at: datetime = <now>
    created_epoch: float = <derived>
    updated_at: datetime = <now>
    updated_epoch: float = <derived>
    version: int = 1                     # bumps on significant mutation
    state: LifecycleState                # plane-specific subset applies
```

Only `namespace` and `state` must be supplied (each subclass gives `state` a default). Every object that exists in Musubi is one of these.

## Memory objects (carry content and relationships)

```python
class MemoryObject(MusubiObject):
    content: str               # non-empty
    summary: str | None = None
    tags: list[str] = []
    importance: int = 5        # 1-10, LLM-scored at maturation
    reinforcement_count: int = 0   # bumped on dedup-hit or reinforce
    last_accessed_at: datetime | None = None
    access_count: int = 0

    # Lineage
    supersedes: list[KSUID]    # this object replaces these
    superseded_by: KSUID | None  # this object has been replaced by
    merged_from: list[KSUID]   # this object was synthesized from these
    linked_to_topics: list[str]  # curated topic slugs
    supported_by: list[ArtifactRef]  # artifact chunk citations
    contradicts: list[KSUID]   # objects this one contradicts
    derived_from: KSUID | None # parent object

    # Validity (see temporal-model)
    valid_from: datetime | None
    valid_until: datetime | None
    valid_from_epoch: float | None
    valid_until_epoch: float | None
```

`MemoryObject` (`base.py:108-184`) is abstract. Concrete types override which fields are required. Every lineage list defaults to empty and every optional pointer to `None`.

### EpisodicMemory

```python
class EpisodicMemory(MemoryObject):
    event_at: datetime         # when the event actually happened
    ingested_at: datetime      # when we learned about it (= created_at typically)
    modality: Literal["text", "voice-transcript", "tool-call", "system-event"]
    participants: list[str]    # presences + humans involved
    source_context: str        # e.g., "Claude Code session 2026-04-17 14:23"
    topics: list[str]
    importance_last_scored_at: datetime | None
    importance_last_scored_epoch: float | None
    retraction_evidence: RetractionEvidence | None
```

See [[04-data-model/episodic-memory]].

### CuratedKnowledge

```python
class CuratedKnowledge(MemoryObject):
    title: str
    topics: list[str]          # primary topical keys
    vault_path: str            # vault-relative path to the .md file
    body_hash: str             # sha256 of the rendered markdown body (content without frontmatter)
    musubi_managed: bool       # if False, promotion writes a sibling instead of this file
    promoted_from: KSUID | None  # if non-null, points to the synthesized concept it came from
    promoted_at: datetime | None
```

### SynthesizedConcept

```python
class SynthesizedConcept(MemoryObject):
    title: str
    synthesis_rationale: str   # LLM-generated: "why these memories cluster"
    promoted_to: KSUID | None  # if promoted, the CuratedKnowledge object_id
    promoted_at: datetime | None
    promotion_rejected_at: datetime | None  # human or rule rejected promotion
    promotion_rejected_reason: str | None
    topics: list[str]
    promotion_attempts: int
    last_reinforced_at: datetime | None
    last_reinforced_epoch: float | None
```

### Thought

```python
class Thought(MusubiObject):
    content: str
    from_presence: str
    to_presence: str              # concrete presence OR "all" for broadcast
    read: bool
    read_by: list[str]
    channel: str = "default"      # named channel (e.g., "scheduler", "ops-alerts")
    importance: int = 5
    in_reply_to: KSUID | None = None
    supersedes: list[KSUID] = []
```

Thoughts carry only minimal lineage, `in_reply_to` and `supersedes` (`src/musubi/types/thought.py:40-41`): they're messages, not knowledge. Importance is still useful for filtering.

## Artifact objects (immutable blobs + chunks)

```python
class SourceArtifact(MusubiObject):
    title: str
    filename: str
    sha256: str
    content_type: str             # MIME
    size_bytes: int
    chunk_count: int
    ingestion_metadata: dict      # source system, URL, uploader, etc.
    chunker: str                  # "markdown-headings-v1", "vtt-turns-v1", "token-sliding-v1"
    artifact_state: Literal["indexing", "indexed", "failed", "stored_unindexed"]
    failure_reason: str | None
    committed_generation: str | None   # committed-generation head
    committed_owner: str | None
    index_operation_id: str | None
    publication_version: int
```

```python
class ArtifactChunk:
    chunk_id: KSUID              # unique per chunk
    artifact_id: KSUID           # parent artifact
    chunk_index: int             # 0-based position
    content: str
    start_offset: int            # into the original artifact (byte or token)
    end_offset: int
    chunk_metadata: dict         # e.g., for VTT: speakers
    generation: str | None       # staging fence
    owner_token: str | None
```

`ArtifactChunk` is not a `MusubiObject` — it lives inside a `SourceArtifact`'s lifecycle. Chunks are not independently versioned.

## Relationship diagram

```
                          supported_by (→ ArtifactRef)
                                    │
                                    ▼
  EpisodicMemory ──merged_from──►  SynthesizedConcept ──promoted_to──► CuratedKnowledge
        │                                                                      │
        │                                                                      │
        └──linked_to_topics──► (topic slugs) ◄──linked_to_topics────────────────┘

        supersedes/superseded_by  (within type, always)

        contradicts → any MemoryObject (cross-type allowed)
```

## Why these specific types

- **EpisodicMemory** = "what happened." Primary write target for adapters.
- **CuratedKnowledge** = "what we believe is true." Primary read target for high-importance queries.
- **SynthesizedConcept** = "what seems to be emerging." Machine-generated hypotheses that may or may not become CuratedKnowledge.
- **SourceArtifact** = "the original source." Never mutated, never summarized (except for metadata).
- **Thought** = "an ambient message between presences." Useful, but not memory per se.

We considered collapsing `SynthesizedConcept` into `EpisodicMemory` with a flag. Rejected because the provenance weight and retrieval behavior are genuinely different (see [[05-retrieval/scoring-model#provenance]]), and the lifecycle (synthesize → promote) is cleaner as a state transition between explicit types.

## ArtifactRef

A small struct, not a full object:

```python
class ArtifactRef:            # src/musubi/types/common.py:140-150
    artifact_id: KSUID
    chunk_id: KSUID | None  # null = whole artifact reference
    quote: str | None       # optional exact-quote excerpt
```

Used wherever a memory cites an artifact.

## ID generation

- All `object_id` values are KSUIDs minted by `svix-ksuid` (`generate_ksuid`, `src/musubi/types/common.py:98-100`). 27 chars, base62, sortable by creation time.
- Qdrant point IDs: a deterministic UUID5 of the KSUID (e.g. `episodic_point_id`, `src/musubi/planes/episodic/plane.py:88-97`; every plane does the same). The KSUID lives in payload as `object_id` and is indexed.
- Two identical artifact uploads get the same `sha256` but different `object_id`s, and are stored as two blobs (no content addressing).

## Test contract for object models (shared)

Every pydantic model in `src/musubi/types/` should have (implemented tests: `tests/types/`):

- `test_<Model>_roundtrip_json` — serialize to JSON and back; equality holds.
- `test_<Model>_roundtrip_qdrant_payload` — convert to Qdrant payload form and back.
- `test_<Model>_schema_version_present` — explicit assertion.
- `test_<Model>_timestamps_validated` — invalid epoch/datetime combos fail validation.
- `test_<Model>_namespace_regex_enforced` — malformed namespaces rejected.
- `test_<Model>_forward_compat_older_schema_reads_ok` — a payload with `schema_version: 1` still parses when `schema_version: 2` is current (applies once a version 2 exists).

## Test Contract

Realized by the model tests in `tests/types/` (`test_base.py`, `test_common.py`, `test_episodic.py`, `test_curated.py`, `test_concept.py`, `test_artifact.py`, `test_thought.py`, `test_lifecycle_event.py`).
