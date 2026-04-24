---
title: Agent-as-tenant namespace convention
section: 13-decisions
tags: [adr, architecture, namespaces, auth, section/decisions, status/accepted, type/adr]
type: adr
status: accepted
decided: 2026-04-24
updated: 2026-04-24
up: "[[13-decisions/index]]"
supersedes:
  - "parts of [[03-system-design/namespaces]] pre-v1.0"
---
# ADR 0030 — Agent-as-tenant

## Context

The namespace shape is `<tenant>/<presence>/<plane>` (3-seg) or `<tenant>/<presence>` (2-seg for cross-plane retrieve). Pre-v1.0 the illustrative convention used **human as tenant**: `eric/nyla/episodic`, `eric/aoi/episodic`, `eric/openclaw/episodic`.

While wiring the openclaw-livekit v0.6.0 cutover and preparing the household-status tool, the human-as-tenant convention produced awkward artifacts:

- Every agent writes under the same tenant prefix, so per-agent scope globs look like `eric/<agent>/*:rw` — fine, but the second-segment name carries two meanings across integrations: in livekit it's the agent identity (`eric/nyla`), in the openclaw plugin it's the bridge identity (`eric/openclaw`).
- "What does Nyla remember?" is naturally a query about an agent, not about Eric's subset of memory under his tenant. The retrieve-side mental model pulls toward agent-as-tenant even when storage is tenant-as-human.
- Scaling to a second instance (another human running Musubi) requires a tenant-prefix convention anyway — so `eric/` was never a universal answer.

## Decision

**Tenant is the agent persona.** Presence is the channel/client the agent is speaking through.

```
<agent>/<channel>/<plane>
```

Examples:

- `nyla/voice/episodic` — Nyla speaking through the LiveKit voice stack.
- `nyla/discord/episodic` — Nyla speaking through Discord text.
- `nyla/openclaw/episodic` — Nyla answering a browser-plugin invocation.
- `aoi/voice/episodic`, `aoi/discord/episodic`.
- 2-seg retrieve: `nyla/voice` → fan across Nyla's voice-plane rows across every plane she's written; `nyla` alone is not a legal namespace (the model requires at least tenant + presence).

Channels in scope for v1.0: `voice`, `discord`, `openclaw`. More as integrations land.

## Scoping model

Tokens follow the tenant-first shape. Each agent gets its own token:

- **Own-write-own-read**: `<agent>/*:rw` (e.g. `nyla/*:rw`).
- **Household-read** (agents that survey other agents, e.g. Nyla, Aoi): `*/episodic:r`, `*/curated:r`, `*/concept:r`, `*/thought:r`. The `*` tenant glob is acceptable because the Musubi instance currently hosts a single human's agent cohort.
- **Cross-tenant write**: forbidden. An agent cannot write into another agent's namespace.
- **Operator tokens**: scope `*:rw` (broad, short-lived, minted on demand for ops / migrations).

### What about multiple humans?

If a second instance lands, we disambiguate by agent-name prefix rather than by adding a tenant wrapper. Possible conventions:

- Per-instance prefix: `eric-nyla`, `lisa-nyla`.
- Per-instance suffix: `nyla-eric`, `nyla-lisa`.
- Multi-tenancy via deploy: separate Musubi instances per human; tenant collision impossible.

Revisit only when a second human actually shows up. Until then, agent-as-tenant is flat and clean.

## Openclaw plugin presence

The openclaw browser plugin bridges for whichever agent is active. Its token's `presence` claim is the plugin's own identity (`openclaw-<machine>`); the request's `namespace` sets `<active-agent>/openclaw/<plane>` at call time. One plugin token per machine, scope `*/openclaw/*:rw` (can write any agent's openclaw namespace) + `*/episodic:r *:r` for retrieve on the agent's behalf.

## Scheduler / system namespaces

`system/lifecycle-worker/*` and `system/scheduler/*` stay under a `system` tenant. Not an agent identity — a reserved slot for lifecycle infrastructure. Tokens with `system:rw` are operator-only.

## Consequences

### Positive

- Queries read naturally: "what does Nyla know?" is `namespace=nyla/…`, not `namespace=eric/nyla/…`.
- Per-agent isolation is first-class in the namespace, not a convention layered on top.
- Cross-channel aggregation within an agent (voice + discord + openclaw) works via 2-seg retrieve (`nyla/`).
- Household surveying uses clean plane-level scope globs (`*/episodic:r`) instead of enumerating 12 agent tenants.

### Trade-offs

- **Scope glob breadth.** `*:r` is wider than `eric/*:r`. Acceptable in a single-instance deploy; revisit if a second instance lands.
- **Migration cost.** Pre-cutover smoke data under `eric/<agent>/*` must be wiped (it's synthetic — no loss). Legacy POC data migrates with a namespace-mapping step in `deploy/migration/poc-to-v1.py`.
- **Vault path convention.** The on-disk vault structure in [[03-system-design/namespaces]] previously partitioned curated files under `vault/curated/<tenant>/`. Under agent-as-tenant this becomes `vault/curated/<agent>/` (Nyla's curated knowledge lives under `vault/curated/nyla/`). Documented in the v1.0 spec refresh.

## Migration

Executed as part of the v1.0 cutover:

1. Wipe canonical Qdrant (synthetic + smoke rows).
2. Re-mint every bearer token under the new convention.
3. Update `AgentConfig` in `openclaw-livekit` (`musubi_v2_namespace = "nyla/voice"`, etc.).
4. Update openclaw plugin tokens + presence defaults.
5. Run `deploy/migration/poc-to-v1.py` with namespace mapping `legacy payload.agent → <agent>/voice/episodic`.
6. Deploy livekit agents against the clean canonical instance.
7. Cut v1.0.0.

## Related

- [[03-system-design/namespaces]] — updated to reflect the new convention.
- [[10-security/auth]] — scope examples updated.
- [[07-interfaces/canonical-api]] / [[07-interfaces/sdk]] / [[07-interfaces/mcp-adapter]] — example namespaces refreshed.
- ADR [[0028-retrieve-2seg-namespace-crossplane]] — 2-seg retrieve still works unchanged; the tenant slot just contains an agent name now.
