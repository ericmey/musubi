---
title: Test Fixtures Catalog
section: _slices
type: index
status: complete
tags: [section/slices, status/complete, type/index, testing]
updated: 2026-04-17
up: "[[_slices/index]]"
reviewed: true
---

# Test Fixtures Catalog

What's already built. Reuse these; don't reinvent. Every fixture below exists in `tests/conftest.py` (or is scheduled to during the schema slice).

## Mocks

| Fixture            | Where                         | Returns / does                                                                  |
|--------------------|-------------------------------|---------------------------------------------------------------------------------|
| `mock_qdrant`      | `tests/conftest.py`           | Minimal in-memory Qdrant shim. Supports `search`, `query`, `scroll`, `set_payload`, `batch_update_points`, `ensure_collections`. |
| `mock_embed`       | `tests/conftest.py`           | Deterministic embedding function. `embed_text("foo") → np.array([...])`. Stable across runs. |
| `FakePoint`        | `tests/conftest.py`           | Builder for Qdrant `ScoredPoint` / `Record` objects with payload helpers.       |
| `FakeQueryResult`  | `tests/conftest.py`           | Wraps a list of `FakePoint` as the server-side `QueryResponse` shape.           |
| `mock_llm`         | *(planned in slice-lifecycle-synthesis)* | Deterministic Qwen2.5-style responses for importance / concept prompts. |
| `frozen_clock`     | `tests/conftest.py`           | Monkeypatches `datetime.now(tz=UTC)` to a fixed value. Use for bitemporal tests.|

## Factories

| Factory                        | Produces                                                                                     |
|--------------------------------|----------------------------------------------------------------------------------------------|
| `make_episodic(**overrides)`   | An `EpisodicMemory` pydantic model with sensible defaults. Override any field.               |
| `make_curated(**overrides)`    | A `CuratedKnowledge` model with frontmatter + body.                                          |
| `make_artifact(**overrides)`   | An `Artifact` with chunk metadata.                                                           |
| `make_concept(**overrides)`    | A `SynthesizedConcept` with lineage pointing at 2+ episodics.                                |
| `make_namespace(tenant, presence)` | A canonical `{tenant}/{presence}/{plane}` namespace string.                             |

## Context managers

| Name                          | Purpose                                                                                   |
|-------------------------------|-------------------------------------------------------------------------------------------|
| `with_vault(tmp_path)`        | Build a temporary Obsidian-like folder tree. Yields a `MusubiVault` bound to it.           |
| `with_fast_clock()`           | Combined with `frozen_clock`; advances the clock in test-local units.                      |

## Integration layer

Integration tests (`tests/integration/`) run against a **dockerized Qdrant**. They:

- Spin up `qdrant:latest` on a random port via the `pytest-docker` plugin.
- Seed a collection using `ensure_collections` + `make_episodic` factories.
- Are skipped in `make test` (unit-only); run in CI as `make test-integration`.

## Property-based (Hypothesis)

Strategies live in `tests/property/strategies.py`:

- `episodic_memory_strategy()` — generates arbitrary episodics respecting invariants (non-empty content, valid namespace, sane timestamps).
- `ranked_hit_list_strategy()` — generates lists of hits for scorer monotonicity checks.

## Adding a fixture

1. Drop it in `tests/conftest.py` (package-wide) or `tests/<area>/conftest.py` (area-scoped).
2. Add a row to this catalog in the same PR.
3. Prefer composition over inheritance — fixtures that take sub-fixtures are fine.

## Anti-patterns (do not add)

- Fixtures that spin up real Qdrant in unit tests (too slow; use `mock_qdrant`).
- Network calls to Gemini or TEI (always mock at the embedding layer).
- Dynamic import of Musubi modules inside a fixture (import at top of `conftest.py`).
- Fixtures that mutate global state without a teardown.
