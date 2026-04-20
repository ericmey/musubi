---
title: "Reading Tour — \"I've never read this vault\""
section: 00-index
type: index
status: complete
tags: [section/index, status/complete, type/index]
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: true
---

# Reading Tour — "I've never read this vault"

This vault came out of a research agent, not hand-authoring. Don't start at `00` and read to `13`. The agent organized the files alphabetically; the numbers are filing, not a reading order.

Instead, follow one of these tours. Each stop has a plain-English prompt for what to look for. Flip `reviewed: true` in frontmatter (or check the Properties panel) as you go — the [[00-index/dashboard|dashboard]] tracks your progress.

## Fast tour (~20 min) — just the spine

Goal: form a mental model. Skim each for headlines; don't sweat details.

1. **[[01-overview/mission]]** — *What problem does Musubi solve?*
2. **[[01-overview/three-planes]]** — *What are the three buckets of memory, and why?*
3. **[[00-index/executive-summary]]** — *Top 5 decisions, top 5 risks, top 5 next steps, in one page.*
4. **[[02-current-state/index]]** — *What already exists (the POC) vs what this doc asks for.*
5. **[[12-roadmap/index]]** — *Where is this going?*

Stop here if you just want a strategy-level grip.

## Standard tour (~60 min) — what an engineer reviews

After the fast tour, add these. Each answers a "how?" question.

6. **[[03-system-design/components]]** — *What are the running processes on the box?*
7. **[[03-system-design/data-flow]]** — *How does a request move through the system?*
8. **[[04-data-model/object-hierarchy]]** — *What shapes of data exist and how do they relate?*
9. **[[04-data-model/lifecycle]]** — *How does a memory move from captured → matured → promoted?*
10. **[[05-retrieval/scoring-model]]** — *How is a search result ranked? What inputs feed the score?*
11. **[[06-ingestion/capture]]** → **[[06-ingestion/lifecycle-engine]]** — *Write path + background jobs.*
12. **[[07-interfaces/canonical-api]]** — *The public contract every adapter consumes.*
13. **[[08-deployment/index]]** — *What it runs on, what it costs, what the blast radius looks like.*
14. **[[09-operations/runbooks]]** — *What does day-2 operation feel like?*
15. **[[13-decisions/index]]** — *Why we chose what we chose. Pairs with the rest.*

## Deep tour — everything else

Once you're comfortable, explore by interest:

- **Security model** — [[10-security/index]]
- **Migration plan** — [[11-migration/index]] + [[11-migration/migration-board]]
- **GPU / model topology** — [[08-deployment/gpu-inference-topology]]
- **Hybrid search internals** — [[05-retrieval/hybrid-search]] + [[05-retrieval/reranker]]
- **Obsidian-as-source-of-truth pattern** — [[06-ingestion/vault-sync]] + [[13-decisions/0003-obsidian-as-sor]]

## As you read

Keep [[_inbox/operator-notes]] open in a side pane. Jot reactions, questions, and disagreements there — don't try to fix specs inline on a first pass.

When you find something missing or wrong, add a checklist item to operator-notes in the form:

```
- [R] [[04-data-model/episodic-memory]] — I don't understand why X. Needs clearer example.
```

The `[R]` marker makes it show up in [[00-index/research-questions]] automatically.

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

## What's next after the tour

- Open [[00-index/dashboard]] — it's the single page you'll return to.
- Open [[00-index/research-questions]] — these are the gaps Eric (or an agent) needs to answer.
- Open the graph (Cmd+G) — colour-coded by status. Red glow = research-needed. Find the dense cluster; that's the heart of the design.
