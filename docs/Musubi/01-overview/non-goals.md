---
title: Non-Goals
section: 01-overview
tags: [overview, scope, section/overview, status/complete, type/overview]
type: overview
status: complete
updated: 2026-04-17
up: "[[01-overview/index]]"
reviewed: false
---
# Non-Goals

What Musubi explicitly will not do. Listed here so slice owners don't drift into scope creep.

## Not a general-purpose vector DB

Musubi is built on Qdrant and uses it extensively, but Musubi is not "a wrapper around Qdrant." External callers that want a raw vector store should use Qdrant directly. Musubi's value-add is the memory semantics: planes, lifecycle, scoring, orchestration.

## Not an agent runtime

Musubi does not execute agent loops. It does not own the LLM. It does not route messages between agents (that is a role for LiveKit, an MCP host, or a framework like Letta). Musubi serves memory and ingests memory. That is all.

The existing `thoughts` subsystem in the POC is a narrow exception — a durable inter-presence message channel — and will be preserved but remains scoped to *durable* messages, not real-time routing.

## Not a multi-org SaaS

Auth, isolation, and ops are designed for a small team sharing one host. Attempting to run Musubi as a per-customer SaaS would require significant rework (per-tenant Qdrant, quota enforcement, billing hooks, SSO, audit-log certification). Explicitly out of scope. Noted in [[10-security/index]].

## Not a document management system

Artifacts are ingested by reference. Musubi stores metadata and chunk embeddings; the canonical file lives in the vault's artifact folder. If you want editing, versioning with diffs, or complex permissioning on documents, use git + your editor; Musubi tracks what you tell it.

## Not a knowledge graph database

We considered a KG-first architecture (Graphiti model) and rejected it for v1. See [[13-decisions/0004-no-knowledge-graph-v1]]. Relationships are tracked as lineage fields on memory objects, and cross-linking via Obsidian wikilinks. A KG can be added later without breaking the data model.

## Not a chat history store

The POC's `thought_history` is preserved but is *not* the canonical record of every chat message. Full chat history lives in the adapter (Claude's conversation memory, LiveKit's session transcript, Discord's channel) or as an artifact (a session export). Musubi stores episodic memories that *distill* conversations, not verbatim logs.

If you want verbatim logs, the artifact plane is the right home — ingest the session transcript as an artifact and let the episodic plane reference chunks of it.

## Not a real-time streaming system

Ingestion is request-response. No Kafka. No streaming update feed to clients. If a client needs to know "what just changed," it polls or uses the (optional, post-v1) change-feed endpoint. This is deliberate: it keeps the system simple to reason about.

## Not a replacement for Obsidian

The vault is the user's primary surface for curated knowledge. Musubi does not build a competing viewer or editor. A future `musubi-studio` web UI may be built for lifecycle/audit browsing, but editing curated knowledge remains Obsidian-first.

## Not an evaluation harness

We test our scoring and retrieval on fixed fixtures (see [[05-retrieval/evals]]), but we do not maintain a general-purpose agent eval harness. For system evals, we recommend integrating with an external harness (e.g., Letta's evaluation tooling).

## Post-v1 (may become goals later)

- Multi-host HA deployment
- Cross-tenant shared-knowledge promotion
- Encrypted-at-rest per-tenant keys (currently relies on host-level disk encryption)
- Fine-grained RBAC (currently: tenant-level only)
- Bring-your-own-embedding via HTTP plugin (currently: named, in-repo adapter)
- Proper knowledge-graph index (currently: lineage fields only)
- Change feed / webhooks
- Voice-capture → artifact pipeline (currently: adapter responsibility)
