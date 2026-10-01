---
title: Roadmap
section: 12-roadmap
tags: [index, roadmap, section/roadmap, status/complete, type/roadmap]
type: roadmap
status: complete
updated: 2026-10-01
up: "[[00-index/index]]"
reviewed: false
---
# Roadmap

Where Musubi is headed after v1. Timelines are loose: this is a thinking roadmap, not a release plan. Work in flight is tracked in GitHub issues and pull requests, and what shipped is in the [releases](https://github.com/sourceblender/musubi/releases) and `CHANGELOG.md`. Repository layout and module boundaries are in `CONTRIBUTING.md` (Repositories).

## Themes

### v1 (shipped)

v1 is the 1.x line (1.27.x at the time of writing): the three planes plus artifacts and thoughts, the Obsidian vault as the curated source of truth, local inference, the canonical HTTP API with a Python SDK, the lifecycle engine, and a single-host Compose deployment. Agent integrations (Claude Code, Codex, OpenClaw, Hermes, OpenCode, Grok, LiveKit) ship as separate plugin repositories.

### Next: proactivity

Once v1 gaps are visible:

- **Proactive thoughts.** Musubi initiates messages to presences when patterns emerge. A pattern-detector lifecycle job, for example: "this topic came up five times this week; want a concept?", "this curated doc hasn't been retrieved in 30 days; archive it?", "three agents hold different versions of X; reconcile?". Needs a rate limit (at most one per day) and per-category mute.
- **Guarded auto-promotion.** Some categories of concept auto-promote without operator approval (tag normalization, terminology fixes, small stylistic rewrites). Larger changes (new concept documents, contradictions) still require human approval. Needs a config-driven policy layer.
- **Richer reflections.** Weekly and monthly reflections in addition to daily, topic-specific reflections, and cross-presence reflection ("coding + voice this week").
- **Mobile capture.** An iOS Shortcut, a messaging adapter or a share-sheet target that hits the capture API. No persistent agent on the phone.
- **Shared team presences.** A teammate's presence, a support bot's presence: separate scopes, an explicit sharing surface.

### Later: exploratory

Directions depend on what the next phase teaches:

- **Federation.** Two Musubi hosts share selected namespaces (for example, two teams in one organisation), perhaps by git-replicating shared vault directories and passing thoughts through a pull-based inbox. Open questions: conflict resolution when both sides edit the same curated doc, discovery (how one presence finds another Musubi), and trust (what stops a malicious peer from reading the wrong namespaces).
- **Offline-first replica.** A laptop holds a read-mostly copy that syncs when online: a small embedded vector index, a vault clone, and a local capture queue.
- **Multi-modal memory.** Image and audio embeddings and search beyond transcripts. Needs larger models and likely more GPU.
- **Better synthesis.** LLM-guided clustering; multi-hop reasoning across concepts.

### Open choices

- **LLM choice.** Re-evaluate the local lifecycle model as models improve; hosted models for some paths only if quality justifies the network hop.
- **Scheduler.** The current scheduler may not scale to multiple hosts.
- **Artifact storage.** Local blob storage is fine for now; object storage at scale.
- **Web presence.** A read-only web view of curated docs is possible but not planned.

### What would force a re-plan

- A dramatically better embedding model that shifts the whole retrieval stack.
- Qdrant becoming incompatible with a feature we depend on.
- A change of direction in an integration Musubi depends on (vendor risk).
- Small-team scale no longer applying: if this grows to a large organisation or a hosted product, rethink.

## Out of scope

Not in v1, not next, probably not ever:

- **Hosted SaaS.** Musubi is designed for self-hosting, and the project does not plan to run a hosted service. Nothing in the license prevents others from doing so.
- **Mobile agent.** Capture yes; a full presence running on a phone needs too much power and network.
- **Per-document ACLs.** Namespace-level is the granularity. Finer would need a permissions engine.
- **Public knowledge sharing.** Musubi is memory for a team's agents, not a publishing platform. Publish curated docs with a separate tool if you need to.

## Non-goals (considered and declined)

- **Building our own vector DB.** Qdrant is the right tool.
- **Writing our own MCP server framework.** FastMCP / the official MCP Python SDK suffice.
- **Replacing Obsidian.** Obsidian is the curated surface; we don't build a web editor.

## Guiding principles for roadmap decisions

1. **One box goes far.** Before adding infra, exhaust single-host optimization.
2. **Curation over convenience.** Features that help the Admin curate memory beat features that make agents more convenient.
3. **Boringly reliable.** Uptime, restore-tested backups, observability > new shiny feature.
4. **Open formats.** Data stays in formats you can take elsewhere.
5. **Small blast radius.** Every change (schema, model, adapter) is reversible. No one-way migrations.
