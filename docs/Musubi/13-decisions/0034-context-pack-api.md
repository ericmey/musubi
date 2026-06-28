---
title: "ADR 0034: Context-pack API for essence alignment"
section: 13-decisions
type: adr
status: accepted
date: 2026-06-28
deciders: [Eric, Aoi, Yua]
tags: [section/decisions, status/accepted, type/adr, musubi-context]
updated: 2026-06-28
up: "[[13-decisions/index]]"
reviewed: false
---

# ADR 0034: Context-pack API for essence alignment

**Status:** accepted
**Date:** 2026-06-28
**Deciders:** Eric, Aoi, Yua

## Context

Musubi already exposes generic retrieval through `/v1/retrieve`, but
Adoption Day v1 identified a sharper product need: agents need a small,
ranked, grouped startup context pack that surfaces essence-aligned facts
without dumping stale or noisy search results into the prompt.

The proven Vice V-049 memory-spine pattern showed that useful context is
not just "nearest text." It needs typed memories, durable-vs-episodic
ranking, staleness suppression, evidence handles, grouped prompt output,
and a hard character cap. If every adapter implements that locally, the
ranking contract will drift and Musubi will become an installed tool that
agents inconsistently use.

## Decision

Add an additive HTTP endpoint, `POST /v1/context`, backed by a pure
`musubi.retrieve.context_pack` domain module.

The endpoint:

- reuses `/v1/retrieve` target resolution, namespace wildcard expansion,
  auth, scope checks, and fast retrieval orchestration;
- adapts retrieval hits into closed-kind context candidates;
- ranks with BM25 lexical relevance plus kind, staleness, importance,
  retrieve-score, and recency tiebreakers;
- suppresses superseded and correction/suppression records by default,
  while allowing explicit `include_history=true` audit retrieval;
- returns a grouped `ContextPack` with evidence handles, why-surfaced
  text, item caps, and character caps;
- serves the same contract to CLI, adapters, and future agent startup
  integrations.

The v1 CLI entrypoint is `musubi context` with a direct script alias
`musubi-context`. It calls the deployed HTTP service; it does not perform
local Qdrant search.

Typed write minimum lands in the same slice: `kind:*` and `staleness:*`
tags are closed-whitelist validated on episodic capture/patch. Legacy
untyped tags remain valid and are read as `kind=episode`.

## Alternatives

### Keep context packing client-side

Rejected. It would duplicate ranking logic across Yua, Aoi, command-chair
scripts, and future adapters, and would make deployed Musubi less valuable
than local agent hacks.

### Extend `/v1/retrieve` with context-pack options

Rejected for v1. `/v1/retrieve` is a general search surface. Context
packing has different response semantics: grouped output, why-surfaced
text, evidence handles, staleness suppression, and startup-mode defaults.
Keeping it separate avoids overloading retrieve while still reusing its
orchestration internally.

### Semantic ranking first

Deferred. The domain module is shaped so semantic scoring can be added
behind the ranking interface later. V1 uses BM25 lexical ranking with
token-overlap fallback behavior because it is deterministic, cheap, and
testable.

## Consequences

- `openapi.yaml` gains `/v1/context` and the `ContextPack*` schemas.
- Adapters can adopt a single deployed context-pack surface instead of
  maintaining local search heuristics.
- Musubi becomes responsible for relevance and staleness discipline, not
  just vector recall.
- Future ranking changes are product behavior changes and should be
  tested against the acceptance scenarios before deployment.
- Deployment close requires blast-radius proof against live consumers,
  not only unit tests: command-chair agents, phone agents, OpenClaw on
  Nyla, and Vice must all pass their existing consumer smokes before and
  after rollout. If any fail, roll back the versioned image pin.
