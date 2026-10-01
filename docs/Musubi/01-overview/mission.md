---
title: Mission
section: 01-overview
tags: [overview, principles, section/overview, status/complete, type/overview]
type: overview
status: complete
updated: 2026-10-01
up: "[[01-overview/index]]"
reviewed: false
---
# Mission

## What Musubi is for

Musubi exists so that **a small team's AI agents share continuity** — across modalities (voice, chat, code), across presences (a coding agent, a voice agent, a chat bot), and across time.

Concretely, it answers four questions for any agent at any moment:

1. **Who am I talking to and what has happened between us?** (Episodic recall.)
2. **What does this team know about this topic?** (Curated lookup.)
3. **What is the ground truth behind this claim?** (Artifact-level RAG.)
4. **What concepts are emerging from our conversations that deserve attention?** (Synthesized concepts + reflection.)

## Design principles (in priority order)

1. **Continuity over completeness.** Returning *something useful* fast beats returning *everything exhaustive* slowly. This is why the fast path exists.
2. **Human-authoritative where it matters.** Curated knowledge is human-edited. Synthesis can *propose* promotions; only a human (or a very narrowly scoped auto-promotion rule) can finalize them.
3. **No silent mutation.** Every change to a memory is versioned, with lineage. We can always explain why a memory looks the way it does.
4. **Self-hosted.** The stack runs on one operator-prepared host with self-hosted models, either on a local GPU or on inference endpoints the operator runs. No third-party cloud service is required in the hot path.
5. **Rebuildable beats backed-up.** Wherever possible, we make derived indices rebuildable from canonical sources (vault + artifacts). This bounds disaster recovery complexity.
6. **Thin adapters, thick core.** Protocol-specific logic stays out of Musubi Core's business logic. Adapters are intentionally boring.
7. **Explicit beats implicit.** Namespaces, lifecycle states, score weights — all first-class and queryable, not conventions.
8. **TDD everywhere.** Every module spec has a test contract. Implementation follows tests.

## Who Musubi is not for

- Not a general-purpose vector DB. It's a memory system built on one.
- Not a multi-org SaaS product. Auth and isolation are sized for a small team, not a tenant of 10,000.
- Not an agent runtime. It does not *run* agents; it *serves memory to* them.
- Not a document management system. Artifacts are ingested by reference; Musubi doesn't own file editing.

See [[01-overview/non-goals]] for more detail.
