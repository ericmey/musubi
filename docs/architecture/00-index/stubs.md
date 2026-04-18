---
title: "Stubs & Placeholders"
section: 00-index
type: index
status: complete
tags: [section/index, status/complete, type/index]
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# Stubs & Placeholders

Notes that are **intentionally brief** — not neglected, just waiting for an upstream decision or dependency. Distinct from [[00-index/research-questions]] which tracks the questions themselves, this page tracks the notes carrying `status: stub`.

Live view: [[_bases/research-stubs]].

## Every stub (live)

```dataview
TABLE WITHOUT ID
  file.link AS "Note",
  section AS "Section",
  up AS "Parent",
  updated AS "Updated"
FROM ""
WHERE status = "stub" AND !contains(file.folder, "_templates") AND !contains(file.folder, "_bases") AND !contains(file.folder, "_inbox")
SORT section ASC, file.name ASC
```

## Every draft (live)

```dataview
TABLE WITHOUT ID
  file.link AS "Note",
  section AS "Section",
  updated AS "Updated"
FROM ""
WHERE status = "draft" AND !contains(file.folder, "_templates") AND !contains(file.folder, "_bases") AND !contains(file.folder, "_inbox")
SORT section ASC, file.name ASC
```

## Intentional stubs

### 11 — Migration phases

The later migration phases are checklists by design. They'll fill in as the preceding phase completes.

- [[11-migration/phase-2-hybrid-search]]
- [[11-migration/phase-3-reranker]]
- [[11-migration/phase-4-planes]]
- [[11-migration/phase-5-vault]]
- [[11-migration/phase-6-lifecycle]]
- [[11-migration/phase-7-adapters]]
- [[11-migration/phase-8-ops]]

### 06 — Ingestion

- [[06-ingestion/index]] — section index (short by design; points to child specs).

## Policy

- A stub has `status: stub` and a **Open questions** section listing what blocks it.
- When the upstream dependency resolves, flip the status to `draft` or `complete`.
- Don't leave stubs un-linked — every stub must be linked from its section index and from here.

## Related

- [[00-index/research-questions]] — where the unknowns live.
- [[00-index/dashboard]] — at-a-glance view of all statuses.
- [[_bases/by-status]] — filterable table.
