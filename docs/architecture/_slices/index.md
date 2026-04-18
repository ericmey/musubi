---
title: Slice Registry
section: _slices
type: index
status: complete
tags: [section/slices, status/complete, type/index]
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: true
---

# Slice Registry

The authoritative list of parallelizable work units. Each slice has:

- an id (`slice-*`),
- a set of **owned paths** (the only files its agent may write),
- a set of **forbidden paths** (owned by other slices),
- **depends-on** / **blocks** edges (walkable via Breadcrumbs),
- a **Definition of Done** checklist.

An agent picks a slice with `status: ready` and all dependencies `done`, locks it, implements it, merges it. See [[00-index/agent-handoff]] for the full protocol.

## Live overview

```dataview
TABLE WITHOUT ID
  file.link AS "Slice",
  phase AS "Phase",
  status AS "Status",
  owner AS "Owner",
  depends-on AS "Depends on",
  blocks AS "Blocks"
FROM "_slices"
WHERE type = "slice"
SORT phase ASC, file.name ASC
```

## By status

```dataview
TABLE WITHOUT ID
  length(rows) AS "count",
  status AS "status"
FROM "_slices"
WHERE type = "slice"
GROUP BY status
SORT status ASC
```

## Ready-to-start (all dependencies done)

```dataview
LIST
FROM "_slices"
WHERE type = "slice" AND status = "ready"
SORT phase ASC, file.name ASC
```

_Note: this simply lists everything `ready`. A dependency-walk would require DataviewJS — track which dependencies are `done` by opening the slice and reading the **Depends on** list._

## Boards & visuals

- [[12-roadmap/slice-board]] — Kanban view.
- [[_slices/slice-dag.canvas|Slice DAG]] — visual dependency graph.
- [[_bases/slices]] — spreadsheet view.

## Supporting docs

- [[00-index/agent-guardrails]] — the rules of engagement.
- [[00-index/agent-handoff]] — the coordination protocol.
- [[00-index/definition-of-done]] — the merge checklist.
- [[_slices/test-fixtures]] — fixtures available to every slice.
- [[_slices/completed-work]] — shipped slices + takeaways.
- [[_tools/README]] — `check.py` validator + `slice_watch.py` notifier.

## Validation

Run at any time to sanity-check the registry:

```bash
python3 _tools/check.py slices     # DAG + owns_paths + locks
python3 _tools/check.py all        # everything (vault + slices + specs)
```

## Catalog

```dataview
TABLE WITHOUT ID
  file.link AS "Slice",
  phase AS "Phase"
FROM "_slices"
WHERE type = "slice"
GROUP BY phase
SORT phase ASC
```
