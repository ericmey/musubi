---
title: Roadmap
section: 12-roadmap
tags: [index, roadmap, section/roadmap, status/complete, type/roadmap]
type: roadmap
status: complete
updated: 2026-04-25
up: "[[00-index/index]]"
reviewed: false
---
# Roadmap

Where Musubi is headed after v1. Timelines are loose — this is a thinking roadmap, not a release plan.

## Docs in this section

- [[12-roadmap/next-up]] — rolling 2-week plan; what's worth building next and in what order.
- [[12-roadmap/public-docs-wiki]] — backlog for building a complete public documentation / wiki surface.
- [[12-roadmap/phased-plan]] — v1 → v2 → v3 direction with bullets per phase.
- [[12-roadmap/ownership-matrix]] — who owns which module + what "ownership" means in a one-person shop.
- [[12-roadmap/status]] — current state of v1 phases, what's in flight, what's next.
- [[12-roadmap/slice-board]] — Kanban board for coding-agent slices.

## Live snapshot

```dataview
TABLE WITHOUT ID
  section AS "Section",
  length(filter(rows, (r) => r.status = "complete")) AS "✅",
  length(filter(rows, (r) => r.status = "draft")) AS "📝",
  length(filter(rows, (r) => r.status = "research-needed")) AS "🔬",
  length(filter(rows, (r) => r.status = "stub")) AS "🧷"
FROM ""
WHERE section AND !contains(file.folder, "_templates") AND !contains(file.folder, "_bases") AND !contains(file.folder, "_inbox") AND !contains(file.folder, "_attachments")
GROUP BY section
SORT section ASC
```

## Themes

### v1 (now → ~Q3 2026)

Everything in sections 01-11. Target: stable single-box household deployment with MCP + LiveKit + OpenClaw adapters.

### v2 (late 2026)

Once v1 is stable and gaps are visible:

- **Proactive thoughts.** Musubi initiates messages to presences when patterns emerge ("you mentioned restarting LiveKit three times this week; want me to make that a runbook?").
- **Richer reflections.** Weekly + monthly reflections in addition to daily. Cross-presence reflection ("coding + voice this week").
- **Guarded auto-promotion.** Some categories of concept auto-promote without operator approval (e.g., tag normalizations). Requires a "policy" layer.
- **Mobile adapter.** iOS shortcut / iMessage capture. A phone-based presence.
- **Shared household presences.** Spouse's presence, kid's presence — separate scopes, explicit sharing surface.

### v3 (2027)

Exploratory; directions depend on v2 learnings:

- **Federation.** Two Musubi hosts share selected namespaces (Eric ↔ household).
- **Offline-first replica.** Laptop has a local read-only replica that syncs when online.
- **Multi-modal memory.** Image / audio-native storage beyond transcripts.
- **Better synthesis.** LLM-guided clustering; multi-hop reasoning across concepts.

## Out of scope forever

Not in v1, not in v2, probably not ever:

- **Hosted SaaS.** We designed for self-host. Don't want to operate someone else's data.
- **Mobile agent.** Capture yes; a full "presence" running on mobile requires too much power/network.
- **Enterprise sales motion.** Not a business.
- **Per-document ACLs.** Namespace-level is the granularity. Finer would need a permissions engine.
- **Public knowledge sharing.** Musubi is for you + collaborators, not for publishing to the internet. (Obsidian Publish works fine for that separately.)

## Non-goals (different: something we've considered and declined)

- **Building our own vector DB.** Qdrant is the right tool.
- **Writing our own MCP server framework.** FastMCP / the official MCP Python SDK suffice.
- **Replacing Obsidian.** Obsidian is the curated surface; we don't build a web editor.

## Guiding principles for roadmap decisions

1. **One box goes far.** Before adding infra, exhaust single-host optimization.
2. **Users > agents.** Features that make humans more effective at curating their memory > features that make agents more convenient.
3. **Boringly reliable.** Uptime, restore-tested backups, observability > new shiny feature.
4. **Open formats.** If we ever migrate off Musubi, export + re-import works. Stays true.
5. **Small blast radius.** Every change (schema, model, adapter) is reversible. No one-way migrations.

## Revisit cadence

Roadmap document revisited quarterly. Phase documents updated in real time as status changes.
