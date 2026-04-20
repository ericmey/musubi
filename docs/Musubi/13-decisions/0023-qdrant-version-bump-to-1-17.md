---
title: "ADR 0023: Qdrant version pin moves from 1.15 to 1.17"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-20
deciders: [Eric]
tags: [section/decisions, status/accepted, type/adr]
updated: 2026-04-20
up: "[[13-decisions/index]]"
reviewed: false
supersedes: ""
superseded-by: ""
---

# ADR 0023: Qdrant version pin moves from 1.15 to 1.17

**Status:** accepted
**Date:** 2026-04-20
**Deciders:** Eric

## Context

The deployment spec ([[08-deployment/index]] §Pinning versions) pins Qdrant at
`qdrant/qdrant:v1.15.0`. When the Musubi host was pre-staged (before any
Ansible/Compose work), the native Qdrant install that was pulled happened to
be **v1.17.1**. Confirmed via `curl http://<musubi-ip>:6333/` on 2026-04-20:

```json
{"title":"qdrant - vector search engine","version":"1.17.1","commit":"eabee3…"}
```

Options:

1. Downgrade the running native Qdrant from 1.17.1 to 1.15.0 before Compose
   migration. Requires data-format compatibility check and a deliberate
   downgrade dance.
2. Accept the installed version and bump the spec pin forward to 1.17.x.

Qdrant's release notes for 1.16.x–1.17.x describe additive features
(multitenancy improvements, binary-quantization ergonomics, HNSW tuning)
without breaking on-disk format changes that would block a Compose re-import
of the data later. No collection exists on the host yet (API returns 401 to
unauthed; confirmed empty state). There is no production data at risk.

## Decision

- **Pin bump:** [[08-deployment/index]] §Pinning versions changes
  `qdrant/qdrant:v1.15.0` → `qdrant/qdrant:v1.17.1`.
- [[04-data-model/qdrant-layout]] HNSW parameters, collection schema
  contracts, and quantization defaults stay the same — 1.17 is a superset.
- The Compose `qdrant` service digest (`<qdrant-digest>` in the operator
  context file) targets 1.17.1 for the first deploy. Future bumps follow the
  normal "bump the pin, bump the digest, verify smoke" loop.
- No data migration is required (no collections exist yet). If/when a migration
  happens between older 1.15 snapshots and 1.17, consult Qdrant's upgrade docs;
  that is a separate ADR.

## Consequences

### Positive

- **Zero-work adoption.** The native install on the host already runs 1.17.1;
  the Compose migration preserves version continuity.
- **Newer features available.** Multitenancy and quantization improvements in
  1.16/1.17 are unblocked for future use.
- **No downgrade fragility.** Downgrading Qdrant across minor versions is
  supported but not recommended by upstream; avoiding it removes risk.

### Negative

- **Spec drift from the original 1.15 reference.** Any docs that hard-code
  `1.15` (search: `grep -rn "1.15" docs/Musubi/`) must be refreshed to `1.17`
  when next edited. Not a blocker; caught at review time.

### Neutral

- The HNSW and quantization defaults remain unchanged; retrieval tests
  continue to cover the canonical behavior shape.

## Alternatives considered

### A. Downgrade the running instance to 1.15

- Why considered: keep the spec-pin stable at its original version.
- Why rejected: the original pin was an arbitrary choice (latest stable at the
  time of spec authoring, 2026-03). 1.17.1 is the current latest stable with no
  known blockers. Downgrading adds ops risk for no architectural gain.

### B. Delay the decision until the Compose migration is actually executed

- Why considered: defer until forced.
- Why rejected: the [[_slices/slice-ops-first-deploy|first-deploy runbook]]
  calls for a concrete pin. Without this ADR, the runbook and the host drift,
  and the first attempt hits the mismatch during Compose up.

## References

- [[08-deployment/index]] §Pinning versions (target of the edit).
- [[04-data-model/qdrant-layout]] — collection schema (unchanged by this ADR).
- [Qdrant 1.17 release notes](https://github.com/qdrant/qdrant/releases) (tag:
  v1.17.1) for the specific changelog between 1.15 and 1.17.
- `.agent-context.local.md` → *Realised deployment state* — records the
  `qdrant --version` output that prompted this ADR.
