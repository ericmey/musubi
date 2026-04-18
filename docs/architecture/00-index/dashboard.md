---
title: Vault Dashboard
section: 00-index
type: index
status: complete
tags: [section/index, status/complete, type/index]
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# Vault Dashboard

Start here for a live, at-a-glance view of vault health. Counts update automatically from frontmatter via Dataview; the spreadsheet equivalents live in [[_bases/index|Bases]].

New to the vault? Take the [[00-index/reading-tour|Reading Tour]]. Jot reactions in [[_inbox/operator-notes|Operator Notes]] as you go. For a visual map of how pieces fit together, open [[00-index/architecture.canvas|Architecture Canvas]]. Coding agents start at [[CLAUDE|CLAUDE.md]].

## Agent work state (live)

```dataview
TABLE WITHOUT ID
  length(rows) AS "count",
  status AS "status"
FROM "_slices"
WHERE type = "slice"
GROUP BY status
SORT status ASC
```

Details: [[_slices/index|Slice Registry]] · [[_slices/slice-dag.canvas|Slice DAG]] · [[_slices/completed-work|Shipped]] · [[12-roadmap/slice-board|Kanban]] · [[_bases/slices|Spreadsheet]] · `python3 _tools/check.py all`

## Review progress (live)

```dataview
TABLE WITHOUT ID
  section AS "Section",
  length(filter(rows, (r) => r.reviewed = true))  AS "✓ read",
  length(filter(rows, (r) => r.reviewed != true)) AS "· unread",
  length(rows) AS "total"
FROM ""
WHERE section AND !contains(file.folder, "_templates") AND !contains(file.folder, "_bases") AND !contains(file.folder, "_inbox") AND !contains(file.folder, "_attachments")
GROUP BY section
SORT section ASC
```

## Status by section (live)

```dataview
TABLE WITHOUT ID
  section AS "Section",
  length(filter(rows.status, (s) => s = "complete"))        AS "✅ complete",
  length(filter(rows.status, (s) => s = "draft"))            AS "📝 draft",
  length(filter(rows.status, (s) => s = "stub"))             AS "🧷 stub",
  length(filter(rows.status, (s) => s = "research-needed")) AS "🔬 research",
  length(rows)                                                AS "total"
FROM ""
WHERE section AND !contains(file.folder, "_templates") AND !contains(file.folder, "_bases") AND !contains(file.folder, "_inbox") AND !contains(file.folder, "_attachments")
GROUP BY section
SORT section ASC
```

## Totals (live)

```dataview
TABLE WITHOUT ID
  length(rows) AS "count",
  status AS "status"
FROM ""
WHERE status AND !contains(file.folder, "_templates") AND !contains(file.folder, "_bases") AND !contains(file.folder, "_inbox")
GROUP BY status
SORT status ASC
```

## Gap hotspots (live)

```dataview
LIST "(" + section + " · " + status + ") — last updated " + updated
FROM ""
WHERE (status = "research-needed" OR status = "stub") AND !contains(file.folder, "_templates") AND !contains(file.folder, "_bases") AND !contains(file.folder, "_inbox")
SORT updated ASC
```

## Recently touched

```dataview
TABLE WITHOUT ID
  file.link AS "Note",
  section AS "Section",
  status AS "Status",
  updated AS "Updated"
FROM ""
WHERE updated AND !contains(file.folder, "_templates") AND !contains(file.folder, "_bases") AND !contains(file.folder, "_inbox")
SORT updated DESC
LIMIT 10
```

## Gap narrative (as of 2026-04-17)

1. **[[05-retrieval/evals]]** — golden sets + RAGAS integration + A/B test harness are TBD.
2. **[[06-ingestion/concept-synthesis]]** — clustering algorithm and fact-extraction prompt are unresolved.
3. **[[09-operations/runbooks]]** — GPU OOM and Qdrant corruption recovery are placeholders.
4. **[[10-security/prompt-hygiene]]** — injection detection patterns are not specified.
5. **[[10-security/audit]]** — audit log ingestion and SIEM integration are WIP.
6. **[[11-migration/phase-1-schema]]** — Pydantic migration details are unresolved.

See [[00-index/research-questions]] for the consolidated research pipeline and [[_bases/research-stubs]] for the live filter.

## Entry points

| Role | Start here |
| --- | --- |
| Coding agent | [[00-index/agent-guardrails]] → [[12-roadmap/phased-plan]] |
| Human reviewer | [[00-index/executive-summary]] |
| Deployment | [[08-deployment/index]] |
| Retrieval implementer | [[05-retrieval/scoring-model]] |
| Adapter author | [[07-interfaces/canonical-api]] → [[07-interfaces/sdk]] |
| Oncall | [[09-operations/runbooks]] |

## Section indexes

- [[01-overview/index]]
- [[02-current-state/index]]
- [[03-system-design/index]]
- [[04-data-model/index]]
- [[05-retrieval/index]]
- [[06-ingestion/index]]
- [[07-interfaces/index]]
- [[08-deployment/index]]
- [[09-operations/index]]
- [[10-security/index]]
- [[11-migration/index]]
- [[12-roadmap/index]]
- [[13-decisions/index]]

## Live views

### Bases (spreadsheet filters over frontmatter)

- [[_bases/by-status]] — every note by status.
- [[_bases/to-review]] — notes you haven't marked `reviewed: true` yet.
- [[_bases/research-stubs]] — open research + stubs.
- [[_bases/drafts]] — draft notes needing follow-through.
- [[_bases/adrs]] — decision records.
- [[_bases/test-contracts]] — specs with Test Contract sections.
- [[_bases/migration-phases]] — POC → v1 phase status.

### Kanban boards (drag-to-move work state)

- [[12-roadmap/slice-board]] — coding-agent slice backlog → in-progress → done.
- [[11-migration/migration-board]] — phase-by-phase migration status.
- [[_inbox/research/research-board]] — research pipeline (proposed → researching → resolved).

### Breadcrumbs

Every note now carries an `up:` field (parent section) and, where relevant,
`next:` / `prev:` / `depends-on:` / `blocks:` / `supersedes:` / `superseded-by:`.
Open the Breadcrumbs side panel (Cmd+P → *Breadcrumbs: Open matrix view*) to
navigate the derived graph. The migration phase chain and ADR supersession
chain are walkable end-to-end from any member.
