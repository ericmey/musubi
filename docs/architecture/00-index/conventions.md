---
title: Conventions
section: 00-index
tags: [reference, section/index, status/complete, style, type/index]
type: index
status: complete
updated: 2026-04-17
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

You never need to memorize "what's in 05." Use the dashboard, the graph, the canvas, or full-text search.

## Review workflow

Every note has a `reviewed:` checkbox in frontmatter. The workflow:

1. Open a note, read it, flip `reviewed` to `true` in the Properties panel (top of the file).
2. As questions occur, append them to [[_inbox/operator-notes]] with a `[R]` checkbox — they show up automatically in [[00-index/research-questions]] and the [[_inbox/research/research-board|Research Board]].
3. Strong questions graduate into their own file under `_inbox/research/` via the **Research Question** template.
4. When a question is answered, update the source spec and either flip the research question to `[x]` or convert it into an ADR in `13-decisions/`.

The [[00-index/dashboard]] shows review progress per section. The [[_bases/to-review]] Base lists everything you haven't marked yet.

## Markdown & vault style

- **Frontmatter is mandatory.** Every note has YAML frontmatter with at minimum `title`, `section`, `tags`. Additional fields per note type below.
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
type: index | spec | runbook | adr | research-question | migration-phase | overview | gap-analysis | roadmap | vault-readme
status: complete | draft | stub | research-needed | living-document | proposed | accepted | superseded | rejected
tags: [section/<slug>, status/<value>, type/<value>, <topical tags...>]
updated: YYYY-MM-DD
---
```

### Optional but encouraged

```yaml
owner: <slice-id | presence | person>    # who is accountable for this note
depends-on: [<wiki-path>, <wiki-path>]   # notes that must be complete first
blocks: [<wiki-path>]                    # what this note blocks
implements: [<spec-path>]                # for code-paired docs
audience: coding-agents | humans         # optional consumer hint
reviewed: true | false                   # has Eric read and mentally accepted this?
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
| `living-document`  | Top-level readmes and dashboards; evergreen.                                         |
| ADR-only: `proposed` / `accepted` / `superseded` / `rejected` — see below.            |

### ADRs (section 13)

```yaml
---
title: "ADR-NNNN: <decision-title>"
section: 13-decisions
type: adr
status: proposed | accepted | superseded | rejected
date: YYYY-MM-DD
deciders: [<name>]
supersedes: <ADR-path>         # optional
superseded-by: <ADR-path>      # if status=superseded
tags: [section/decisions, status/<value>, type/adr]
updated: YYYY-MM-DD
---
```

### Curated knowledge (in Obsidian vault, not this architecture vault)

```yaml
---
id: <ksuid>
musubi-managed: true|false
plane: curated
state: matured | promoted | demoted | archived
topics: [<topic1>, <topic2>]
sources: [<artifact-id>, <artifact-id>]
promoted-from: <synthesized-concept-id>   # optional
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
- **Qdrant collections:** `musubi_<plane>_<namespace_hash_or_literal>`. Example: `musubi_episodic_default`. Aliases preserve legacy names.
- **gRPC services:** `<verb>_<noun>` methods within `MusubiService` (e.g., `StoreEpisodic`, `QueryBlended`).
- **REST routes:** plural nouns, kebab-case. `POST /v1/episodic-memories`, `GET /v1/curated-knowledge/{id}`.

## IDs

- **All object IDs are KSUIDs** (27-char sortable). Not UUIDs. This gives us k-sortable time-prefixed IDs for cheap lexicographic recency queries at the object-store layer.
- Qdrant point IDs can stay UUID if Qdrant requires them; the KSUID lives in payload as `object_id`. We query by `object_id` index.
- Citation form in docs: `{plane}/{object_id}` (e.g., `episodic/2W1eP3rZaLlQ4jT...`).

## Testing conventions

- **Test files mirror source paths.** `src/musubi/retrieve/scoring.py` → `tests/retrieve/test_scoring.py`.
- **Test names are assertions.** `test_fast_path_excludes_provisional_memories`, not `test_fast_path_1`.
- **Test-name convention (enforced):** when a spec has a `## Test Contract` section, **test function names transcribe the bullet text verbatim** with `_` for spaces and no paraphrasing. The spec is the authoring source; the test name is the mechanical copy. This is how [[00-index/agent-guardrails#Test Contract Closure Rule]] is auditable — a grep over `tests/` against the spec bullet list shows silent omissions immediately.

  Example:

  ```
  # In docs/architecture/04-data-model/episodic-memory.md §Test contract:
  - test_create_sets_provisional_state
  - test_create_dedup_hit_updates_existing_instead_of_inserting
  - test_patch_tags_is_additive_by_default

  # In tests/planes/test_episodic.py (matching verbatim):
  def test_create_sets_provisional_state(...): ...
  def test_create_dedup_hit_updates_existing_instead_of_inserting(...): ...
  @pytest.mark.skip(reason="deferred to slice-plane-episodic follow-up: patch not yet implemented")
  def test_patch_tags_is_additive_by_default(...): ...
  ```

- **Each module spec has a "Test Contract" section** listing the behaviors that must be tested. At handoff, every bullet is in one of the three Closure states defined in [[00-index/agent-guardrails#Test Contract Closure Rule]].
- **Fixtures** live in `tests/conftest.py` (package-wide) or `tests/<area>/conftest.py` (area-specific).
- **No external services in unit tests.** Qdrant runs in-memory via `QdrantClient(":memory:")`; TEI / Gemini / Ollama are mocked (see the FakeEmbedder pattern in `src/musubi/embedding/fake.py`).
- **Integration tests** go in `tests/integration/` and can hit a dockerized Qdrant + real TEI. They run in CI but not in `make test`.
- **Coverage target:** 85 % branch coverage on owned files (90 % on `src/musubi/planes/**` and `src/musubi/retrieve/**`). Enforced via `fail_under = 85` in `pyproject.toml` `[tool.coverage.report]`. Thin wrappers (API routing, CLI main) excluded via `[tool.coverage.run].omit`.

## Commits & PRs

- **Conventional Commits.** `feat(retrieval): add hybrid scoring`, `fix(lifecycle): debounce vault writes`, `docs(05): clarify fast-path budget`.
- **PR titles mirror the primary commit.**
- **PR description template** lives at `.github/pull_request_template.md` and includes:
  - Slice ID
  - Spec references (`Implements: 05-retrieval/scoring-model.md §Weighted score`)
  - Test contract coverage checklist
  - Screenshots/logs for behavioral changes
  - Rollback plan
- **Commits that change the spec** must be tagged with `spec-update: <doc-path>` in the trailer.

## Versioning

- **Musubi Core**: SemVer. v0.x until API v1.0 is declared frozen.
- **Canonical API**: independent SemVer path. `/v1/...` URL prefix; `musubi.v1.*` proto package. Breaking changes produce `/v2/`.
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
| `section/` | `index`, `overview`, `current-state`, `system-design`, `data-model`, `retrieval`, `ingestion`, `interfaces`, `deployment`, `operations`, `security`, `migration`, `roadmap`, `decisions` |
| `status/`  | `complete`, `draft`, `stub`, `research-needed`, `living-document`, `proposed`, `accepted`, `superseded`, `rejected` |
| `type/`    | `index`, `spec`, `runbook`, `adr`, `research-question`, `migration-phase`, `overview`, `gap-analysis`, `roadmap`, `vault-readme` |

Free-form **topical tags** (e.g. `retrieval`, `planes`, `gpu`) are allowed
alongside the namespaced ones. Obsidian's tag pane nests them automatically.

## Vault structure

```
musubi/
├── README.md
├── 00-index/            navigation, glossary, conventions, dashboards
├── 01-overview/         mission, personas, three planes, research grounding
├── 02-current-state/    POC inventory + gap analysis
├── 03-system-design/    components, topology, failure modes
├── 04-data-model/       object schemas + lifecycle
├── 05-retrieval/        scoring, hybrid search, fast/deep paths
├── 06-ingestion/        capture, maturation, synthesis, promotion, vault-sync
├── 07-interfaces/       canonical API, SDK, adapters, contract tests
├── 08-deployment/       Ansible, Docker Compose, GPU topology
├── 09-operations/       runbooks, alerts, capacity, backup-restore
├── 10-security/         auth, redaction, audit, data handling
├── 11-migration/        phase-by-phase POC → v1 plan
├── 12-roadmap/          v1/v2/v3 direction, ownership, status
├── 13-decisions/        ADRs + sources
│
├── _templates/          Templater templates (spec, adr, runbook, ...)
├── _bases/              Obsidian Bases (dynamic views over frontmatter)
├── _inbox/              transient — research questions, cross-slice tickets, locks
│   ├── research/        open research questions (use research-question template)
│   ├── cross-slice/     coordination tickets between agent slices
│   ├── locks/           lockfiles for single-agent-per-module discipline
│   └── questions/       agent-filed blockers
├── _attachments/        images and binary drop-ins (kept out of the main graph)
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
| **Dataview** | Live tables over frontmatter inside dashboards and section indexes. DataviewJS is enabled. |
| **Breadcrumbs** | Interprets `up:` / `next:` / `prev:` / `depends-on:` / `blocks:` / `supersedes:` / `superseded-by:` as graph edges. Reverse edges are implied automatically. |
| **Kanban** | Boards at [[12-roadmap/slice-board]], [[11-migration/migration-board]], [[_inbox/research/research-board]]. |
| **Local REST API** | Programmatic access to the vault from Musubi's own vault-sync. |
| **Style Settings** | Lets you tune the `musubi-status-colors` CSS snippet without editing files. |
| **Bases** (core) | Spreadsheet-style views over frontmatter. Used for all status dashboards. |
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

- All live tables in dashboards use the source expression `FROM ""` and filter
  out infra folders (`_templates`, `_bases`, `_inbox`, `_attachments`). Copy
  the existing queries when adding new ones.
- Inline queries use `=` prefix; inline JS uses `$=`.
- Avoid DataviewJS inside committed docs unless it adds obvious value — JS
  queries are harder for agents to reason about.

### Kanban conventions

- Boards are authored in markdown and have `type: kanban`, `kanban-plugin:
  board` in frontmatter. Lanes are H2 headings; cards are checklist items.
- Cards reference the underlying spec with a wikilink so Breadcrumbs/graph
  still see the connection.
- Keep the lane set small and consistent: **Backlog / In progress / In review
  / Done** for roadmap; **Not started / In progress / Blocked / Done** for
  migration; **Proposed / Researching / Writing up / Resolved** for research.

See [[README#Recommended extras]] for plugins worth adding as the vault grows.
