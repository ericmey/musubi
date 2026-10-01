---
title: Conventions
section: 00-index
tags: [reference, section/index, status/complete, style, type/index]
type: index
status: complete
updated: 2026-10-01
up: "[[00-index/index]]"
reviewed: false
---
# Conventions

Rules of the road for writing, naming, and organizing across this vault and the codebase.

## A note on section numbering

The `00–13` folder prefixes are **filing, not a reading plan**. They came from a research agent that needed deterministic ordering; they do not encode a story you have to read in order. The actual reading order is captured by:

- [[00-index/reading-tour]] for humans reviewing the vault.
- Breadcrumbs frontmatter (`up`, `next`, `prev`, `depends-on`, `blocks`, `supersedes`) for the walkable graph.
- [[00-index/architecture.canvas]] for the visual map of how components relate.

You never need to memorize "what's in 05." Use the index pages, the graph, the canvas, or full-text search.

## Review workflow

Every note has a `reviewed:` checkbox in frontmatter. The workflow:

1. Open a note, read it, flip `reviewed` to `true` in the Properties panel (top of the file).
2. As questions occur, add them to the note you're reading with an `[R]` checkbox — they show up automatically in [[00-index/research-questions]].
3. Strong questions graduate into a GitHub issue labelled `research`.
4. When a question is answered, update the source spec and either flip the research question to `[x]` or convert it into an ADR in `13-decisions/`.

## Markdown & vault style

- **Frontmatter is mandatory.** Every note has YAML frontmatter with at minimum `title`, `section`, `type`, `status`, `tags` and `updated` (`_tools/check.py` fails a note missing any of them). Additional fields per note type below.
- **One H1 per note**, matching the `title` frontmatter.
- **Use wikilinks `[[path/file]]`** for intra-vault references. Use regular markdown links `[label](url)` only for external URLs.
- **Link liberally.** If you mention a concept defined elsewhere, link it. A navigable knowledge graph is the goal.
- **No isolated documents.** Every new note must be linked from at least one index.md and one other note.
- **ASCII diagrams only** (no Mermaid, no embedded images). This keeps the vault plugin-free and round-trips through plain markdown.
- **Tables use standard markdown.** Keep them narrow enough to read unwrapped (< 120 char rows).

## Frontmatter schema

Every note in this vault carries the fields below. The linter
([[00-index/conventions#Linter|Linter config]]) enforces key ordering and
deduplicates tags. The property panel in the sidebar surfaces these fields as
typed inputs; see `.obsidian/types.json`.

### Required on every note

```yaml
---
title: <human-readable>
section: <NN-section-slug>              # matches parent folder
type: index | spec | contract | runbook | adr | migration-phase | overview | roadmap | vault-readme
status: complete | draft | stub | research-needed | living-document | proposed | accepted | superseded | rejected
tags: [section/<slug>, status/<value>, type/<value>, <topical tags...>]
updated: YYYY-MM-DD
---
```

### Optional but encouraged

```yaml
owner: <person | team>                  # who is accountable for this note
depends-on: [<wiki-path>, <wiki-path>]   # notes that must be complete first
blocks: [<wiki-path>]                    # what this note blocks
implements: [<spec-path>]                # for code-paired docs
audience: coding-agents | humans         # optional consumer hint
reviewed: true | false                   # has the maintainer read and accepted this?
```

### Breadcrumbs (navigation graph)

```yaml
up: "[[section-index]]"         # parent
next: "[[next-in-chain]]"        # optional — e.g. migration phases
prev: "[[previous-in-chain]]"
supersedes: "[[older-adr]]"      # ADRs only
superseded-by: "[[newer-adr]]"
```

### `status` values

| Value              | Meaning                                                                              |
|--------------------|--------------------------------------------------------------------------------------|
| `complete`         | Fully specified. Any remaining questions are scoped in **Open questions** as non-blocking. |
| `draft`            | Mostly written, but has open questions that could change the spec.                   |
| `stub`             | Intentionally brief placeholder; link to upstream blocker in **Open questions**.     |
| `research-needed`  | Contains a research-blocker that must be answered before the note can progress.       |
| `living-document`  | Top-level readmes and index pages; evergreen.                                        |
| ADR-only: `proposed` / `accepted` / `superseded` / `rejected` — see below.            |

### ADRs (section 13)

```yaml
---
title: "ADR-NNNN: <decision-title>"
section: 13-decisions
type: adr
status: proposed | accepted | superseded | rejected
date: YYYY-MM-DD
supersedes: <ADR-path>         # optional
superseded-by: <ADR-path>      # if status=superseded
tags: [section/decisions, status/<value>, type/adr]
updated: YYYY-MM-DD
---
```

### Curated knowledge (in the knowledge vault at `VAULT_PATH`, not this docs vault)

An abridged view of `CuratedFrontmatter` (`src/musubi/vault/frontmatter.py`):

```yaml
---
object_id: <ksuid>                        # optional on a new human-authored file
namespace: <tenant>/<presence>/curated
title: <human-readable>
musubi-managed: true|false
state: matured | promoted | demoted | archived | superseded
topics: [<topic1>, <topic2>]
supported_by: [{artifact_id: <ksuid>}]
promoted_from: <synthesized-concept-id>   # optional
created: ISO8601
updated: ISO8601
version: <int>
---
```

See [[06-ingestion/vault-frontmatter-schema]] for the full spec.

## Naming

- **Files in this vault:** kebab-case, prefixed by section number only if inside a section root (e.g., `05-retrieval/scoring-model.md` not `05-retrieval/05-scoring-model.md`).
- **Python modules:** snake_case. Directory names snake_case.
- **Python classes:** PascalCase. No trailing `Impl`, `Base`, `Manager`, etc. unless the semantic is real.
- **Qdrant collections:** one collection per plane, not per namespace: `musubi_episodic`, `musubi_curated`, `musubi_concept`, `musubi_artifact` (metadata), `musubi_artifact_chunks`, `musubi_thought` and `musubi_lifecycle_events` (`src/musubi/store/names.py`). The namespace is a payload field filtered at query time.
- **REST routes:** plane-aligned names per [ADR-0029](../13-decisions/0029-plane-aligned-endpoint-paths.md). The plane vocabulary is authoritative: `POST /v1/episodic`, `GET /v1/curated/{id}`, `POST /v1/concepts/{id}/promote`, `POST /v1/artifacts`.

## IDs

- **All object IDs are KSUIDs** (27-char sortable). Not UUIDs. This gives us k-sortable time-prefixed IDs for cheap lexicographic recency queries at the object-store layer.
- Qdrant point IDs can stay UUID if Qdrant requires them; the KSUID lives in payload as `object_id`. We query by `object_id` index.
- Citation form in docs: `{plane}/{object_id}` (e.g., `episodic/2W1eP3rZaLlQ4jT...`).

## Testing conventions

- **Test files mirror source paths.** `src/musubi/retrieve/scoring.py` → `tests/retrieve/test_scoring.py`.
- **Test names are assertions.** `test_fast_path_excludes_provisional_memories`, not `test_fast_path_1`.
- **Test-name convention (enforced):** when a spec has a `## Test Contract` section, **test function names transcribe the bullet text verbatim** with `_` for spaces and no paraphrasing. The spec is the authoring source; the test name is the mechanical copy. This is how the Test Contract Closure Rule in [AGENTS.md](../../../AGENTS.md#test-contract-closure-rule) is auditable — a grep over `tests/` against the spec bullet list shows silent omissions immediately.

  Example:

  ```
  # In docs/Musubi/04-data-model/episodic-memory.md §Test contract:
  - test_create_sets_provisional_state
  - test_create_dedup_hit_updates_existing_instead_of_inserting
  - test_patch_tags_is_additive_by_default

  # In tests/planes/test_episodic.py (matching verbatim):
  def test_create_sets_provisional_state(...): ...
  def test_create_dedup_hit_updates_existing_instead_of_inserting(...): ...
  @pytest.mark.skip(reason="deferred to #<issue>: patch not yet implemented")
  def test_patch_tags_is_additive_by_default(...): ...
  ```

- **Each module spec has a "Test Contract" section** listing the behaviors that must be tested. At handoff, every bullet is in one of the three closure states defined in [AGENTS.md](../../../AGENTS.md#test-contract-closure-rule).
- **Fixtures** live in `tests/conftest.py` (package-wide) or `tests/<area>/conftest.py` (area-specific).
- **No external services in unit tests.** Qdrant runs in-memory via `QdrantClient(":memory:")`; TEI and Ollama are mocked (see the FakeEmbedder pattern in `src/musubi/embedding/fake.py`).
- **Integration tests** carry the `integration` marker and hit a dockerized Qdrant, TEI and Ollama. The default `pytest` run deselects them (`-m 'not integration'` in `pyproject.toml`); `make test-integration` and the integration workflow run them.
- **Coverage target:** 85 % branch coverage, enforced via `fail_under = 85` in `pyproject.toml` `[tool.coverage.report]`. The 90 % expectation on `src/musubi/planes/**` and `src/musubi/retrieve/**` is checked in review. Only the package `__init__.py` files are omitted (`[tool.coverage.run].omit`).

## Commits & PRs

- **Conventional Commits.** `feat(retrieval): add hybrid scoring`, `fix(lifecycle): debounce vault writes`, `docs(05): clarify fast-path budget`.
- **PR titles mirror the primary commit.**
- **PR description template** lives at `.github/pull_request_template.md` and has these sections:
  - Summary
  - Test Contract coverage (required when a spec is implemented)
  - Definition of Done
  - Agent attribution
  - Risk + rollback
- **Commits that change the spec** must be tagged with `spec-update: <doc-path>` in the trailer.

## Versioning

- **Musubi Core**: SemVer, currently v1.x (`version` in `pyproject.toml`), managed by release-please.
- **Canonical API**: `/v1/...` URL prefix. Breaking changes produce `/v2/`.
- **SDK**: major version tracks API major version.
- **Adapters**: independent SemVer; pin to an SDK version range.
- **Schemas**: every Qdrant payload has a `schema_version: int` field. Reader is forward-compatible; writer always writes latest.

## Time & timestamps

- **Always UTC ISO8601 with microseconds.** `2026-04-17T14:23:02.123456Z`.
- **Plus a `*_epoch` float** for Qdrant range filters.
- **Never** use `datetime.now()` without `tz=UTC`.

## Tag taxonomy

Tags in this vault are **namespaced** — they read like short hierarchies
(`status/complete`, `type/adr`). Namespaces are enforced so that searches and
graph filters stay precise.

| Namespace  | Values                                                                          |
|------------|---------------------------------------------------------------------------------|
| `section/` | `index`, `overview`, `system-design`, `data-model`, `retrieval`, `ingestion`, `interfaces`, `deployment`, `operations`, `security`, `migration`, `roadmap`, `decisions` |
| `status/`  | `complete`, `draft`, `stub`, `research-needed`, `living-document`, `proposed`, `accepted`, `superseded`, `rejected` |
| `type/`    | `index`, `spec`, `contract`, `runbook`, `adr`, `migration-phase`, `overview`, `roadmap`, `vault-readme` |

Free-form **topical tags** (e.g. `retrieval`, `planes`, `gpu`) are allowed
alongside the namespaced ones. Obsidian's tag pane nests them automatically.

## Vault structure

```
docs/Musubi/
├── README.md
├── CLAUDE.md            coding-agent entry point
├── 00-index/            navigation, glossary, conventions
├── 01-overview/         mission, personas, three planes, research grounding
├── 03-system-design/    components, topology, failure modes
├── 04-data-model/       object schemas + lifecycle
├── 05-retrieval/        scoring, hybrid search, fast/deep paths
├── 06-ingestion/        capture, maturation, synthesis, promotion, vault-sync
├── 07-interfaces/       canonical API, SDK, adapters, contract tests
├── 08-deployment/       Docker Compose install, host requirements, GPU topology
├── 09-operations/       runbooks, alerts, capacity, backup-restore
├── 10-security/         auth, redaction, audit, data handling
├── 11-migration/        schema and re-embedding migrations
├── 12-roadmap/          longer-range direction
├── 13-decisions/        ADRs + sources
│
├── _templates/          Templater templates (spec, adr, runbook, ...)
├── _tools/              check.py, the docs health check
├── proto/               protobuf reference mirror (no server)
└── .obsidian/           vault config; committed to git
```

Folders prefixed with `_` are intentionally excluded from the linter's normal
sweep (see `foldersToIgnore` in `obsidian-linter/data.json`) and sorted to the
top of the file explorer alphabetically. Treat them as infrastructure, not
content.

## Linter

`obsidian-linter` runs **on save** (not on file change, so auto-watchers don't
churn). The enabled rules are chosen to be non-destructive: they format YAML,
trim whitespace, normalise list markers, and sort frontmatter keys by the
priority order `title, section, type, status, owner, tags, updated, depends-on`.
Rules that would rewrite titles, escape YAML values, or capitalize headings are
intentionally disabled — our content uses technical casing the linter does not
understand.

## Plugin stack

This vault assumes the following plugins are installed. The `.obsidian/` config
has been tuned for them.

| Plugin | Role |
|---|---|
| **Templater** | New notes in each section scaffold from `_templates/`. |
| **Linter** | On-save frontmatter + markdown normalisation. |
| **Tasks** | Tracks roadmap / research checklists across files. Custom statuses include `R` (research). |
| **Dataview** | Live tables over frontmatter inside section indexes and index pages. DataviewJS is enabled. |
| **Breadcrumbs** | Interprets `up:` / `next:` / `prev:` / `depends-on:` / `blocks:` / `supersedes:` / `superseded-by:` as graph edges. Reverse edges are implied automatically. |
| **Local REST API** | Enabled in `.obsidian/` for editor tooling. Musubi does not use it; vault sync reads the filesystem. |
| **Style Settings** | Lets you tune the `musubi-status-colors` CSS snippet without editing files. |
| **Bases** (core) | Spreadsheet-style views over frontmatter. Available; the repo ships none. |
| **Graph, Backlinks, Outgoing Links, Properties, Canvas** (core) | Enabled and configured. |

### Breadcrumbs fields

The vault uses Breadcrumbs' default hierarchy fields plus these extras:

| Field          | Meaning                                                                 |
|----------------|-------------------------------------------------------------------------|
| `up`           | Parent note (section index → root index → vault). Present on every note.|
| `next` / `prev`| Linear chain (used on migration phases; optional elsewhere).            |
| `same`         | Sibling cluster (rarely used; reserved for cross-refs).                 |
| `depends-on`   | Notes that must be `status: complete` first.                            |
| `blocks`       | Reverse of `depends-on`; auto-derived.                                   |
| `supersedes`   | For ADRs; which prior ADR this replaces.                                 |
| `superseded-by`| For ADRs; which newer ADR replaces this.                                 |
| `implements`   | Spec-to-code link for paired implementation docs.                        |

### Dataview conventions

- All live tables use the source expression `FROM ""` and filter out infra
  folders such as `_templates`. Copy
  the existing queries when adding new ones.
- Inline queries use `=` prefix; inline JS uses `$=`.
- Avoid DataviewJS inside committed docs unless it adds obvious value — JS
  queries are harder for agents to reason about.

See [[README#Recommended extras]] for plugins worth adding as the vault grows.
