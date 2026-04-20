---
title: "ADR 0001: Three-Plane Architecture"
section: 13-decisions
tags: [adr, architecture, planes, section/decisions, status/accepted, type/adr]
type: adr
status: accepted
updated: 2026-04-17
up: "[[13-decisions/index]]"
reviewed: false
---
# ADR 0001: Three-Plane Architecture

**Status:** accepted
**Date:** 2026-03-14
**Deciders:** Eric

## Context

Musubi needs to store several kinds of knowledge: raw observations, polished summaries, background inferences, uploaded source material. The POC mashes them all into one Qdrant collection with flag fields. That works at POC scale but collapses at any real growth: retention rules can't be expressed, retrieval is noisy, lifecycle becomes a pile of if-statements.

Prior art:

- Letta / MemGPT: a tiered memory (core / archival / recall) modeled on OS memory hierarchy.
- Mem0: extract / update / summarize phases with explicit state transitions.
- Zep: bitemporal relational store; separate layers for facts, entities, relationships.
- Stanford Generative Agents: separate memory streams with reflection-generated meta-memories.

All of them separate "what was observed" from "what was concluded" from "what is permanent." Different names, same idea.

## Decision

Musubi stores data across **three parallel planes** plus **one support plane**:

1. **Episodic Memory** — raw first-person observations, machine-captured.
2. **Curated Knowledge** — polished, human-authored or human-reviewed; lives in Obsidian.
3. **Synthesized Concept** — machine-generated hypotheses bridging episodic and curated.
4. (Support) **Source Artifact** — raw uploaded material (PDFs, pages, transcripts) that backs the above.

Each plane has its own Qdrant collection, its own retention rules, its own retrieval expectations.

## Alternatives

**A. Single collection, type flag.** Our POC. Simple, but retention + retrieval logic fights the data.

**B. Two planes (episodic + curated).** Skips concepts. Works, but then reinforcement + synthesis has no place to live; every episodic memory has to carry "will this become a fact?" state in its payload.

**C. Hierarchical tiers** (hot / warm / cold storage). Mismatches our needs — we don't have latency tiers; we have semantic tiers. See [[13-decisions/0002-planes-not-tiers]].

**D. Relational store with joins.** Postgres + pgvector. Would let us model relationships strictly. But we don't need strict joins, and pgvector doesn't match Qdrant's hybrid + named-vector + quantization capabilities as of April 2026.

## Consequences

- Collections multiply (5 for v1: episodic, curated, concept, artifact_chunks, thoughts). That's fine — Qdrant handles many collections easily.
- Scoring has per-plane provenance adjustments ([[05-retrieval/scoring-model]]).
- Lifecycle jobs split naturally by plane (maturation per episodic, synthesis spans episodic→concept, promotion spans concept→curated).
- Retrieval can span planes ([[05-retrieval/blended]]) or target one.
- Human curation has a clear home (curated plane = vault).

Trade-offs:

- Cross-plane joins at query time (blended) add complexity vs a single collection. Handled via retrieval orchestration, not at storage level.
- Migration takes a phase of work ([[11-migration/phase-4-planes]]).

## Links

- [[01-overview/three-planes]]
- [[13-decisions/0002-planes-not-tiers]]
- [[13-decisions/sources]]
