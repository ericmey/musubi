---
title: ADR 0030 — Agent-as-tenant
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

The namespace shape is `<tenant>/<presence>/<plane>` (3-seg) or `<tenant>/<presence>` (2-seg for cross-plane retrieve). Pre-v1.0 the illustrative convention used **human as tenant**: `admin/alex/episodic`, `admin/sam/episodic`, `admin/openclaw/episodic`.

While wiring the openclaw-livekit v0.6.0 cutover and preparing the household-status tool, the human-as-tenant convention produced awkward artifacts:

- Every agent writes under the same tenant prefix, so per-agent scope globs look like `admin/<agent>/*:rw` — fine, but the second-segment name carries two meanings across integrations: in livekit it's the agent identity (`admin/alex`), in the openclaw plugin it's the bridge identity (`admin/openclaw`).
- "What does Alex remember?" is naturally a query about an agent, not about the Admin's subset of memory under the Admin tenant. The retrieve-side mental model pulls toward agent-as-tenant even when storage is tenant-as-human.
- Scaling to a second instance (another human running Musubi) requires a tenant-prefix convention anyway — so `admin/` was never a universal answer.

## Decision

**Tenant is the agent persona.** Presence is the channel/client the agent is speaking through.

```
<agent>/<channel>/<plane>
```

Examples:

- `alex/voice/episodic` — Alex speaking through the LiveKit voice stack.
- `alex/discord/episodic` — Alex speaking through Discord text.
- `alex/openclaw/episodic` — Alex answering a browser-plugin invocation.
- `sam/voice/episodic`, `sam/discord/episodic`.
- 2-seg retrieve: `alex/voice` → fan across Alex's voice-plane rows across every plane it has written; `alex` alone is not a legal namespace (the model requires at least tenant + presence).

Channels in scope for v1.0: `voice`, `discord`, `openclaw`. More as integrations land.

## Scoping model

Tokens follow the tenant-first shape. Each agent gets its own token:

> **Note (2026-10-01):** scope globs now match segment-for-segment against the namespace (`src/musubi/auth/scopes.py`, `_namespace_matches`), so the 2-segment globs below are historical shorthand. Current equivalents: `alex/*/*:rw` for own-write-own-read, `*/*/episodic:r` for cross-agent read.

- **Own-write-own-read**: `<agent>/*:rw` (e.g. `alex/*:rw`).
- **Cross-agent read** (agents that survey other agents, e.g. Alex, Sam): `*/episodic:r`, `*/curated:r`, `*/concept:r`, `*/thought:r`. The `*` tenant glob is acceptable for a single-operator deployment.
- **Cross-tenant write**: forbidden. An agent cannot write into another agent's namespace.
- **Operator tokens**: scope `*:rw` (broad, short-lived, minted on demand for ops / migrations).

### What about multiple humans?

If a second instance lands, we disambiguate by agent-name prefix rather than by adding a tenant wrapper. Possible conventions:

- Per-instance prefix: `admin-alex`, `other-alex`.
- Per-instance suffix: `alex-admin`, `alex-other`.
- Multi-tenancy via deploy: separate Musubi instances per human; tenant collision impossible.

Revisit only when a second human actually shows up. Until then, agent-as-tenant is flat and clean.

## Openclaw plugin presence

The openclaw browser plugin bridges for whichever agent is active. Its token's `presence` claim is the plugin's own identity (`openclaw-<machine>`); the request's `namespace` sets `<active-agent>/openclaw/<plane>` at call time. One plugin token per machine, scope `*/openclaw/*:rw` (can write any agent's openclaw namespace) + `*/episodic:r *:r` for retrieve on the agent's behalf.

## Scheduler / system namespaces

`system/lifecycle-worker/*` and `system/scheduler/*` stay under a `system` tenant. Not an agent identity — a reserved slot for lifecycle infrastructure. Tokens with `system:rw` are operator-only.

## Consequences

### Positive

- Queries read naturally: "what does Alex know?" is `namespace=alex/…`, not `namespace=<human>/alex/…`.
- Per-agent isolation is first-class in the namespace, not a convention layered on top.
- Cross-channel aggregation within an agent (voice + discord + openclaw) works via 2-seg retrieve (`alex/`).
- Cross-agent surveying uses clean plane-level scope globs (`*/episodic:r`) instead of enumerating every agent tenant.

### Trade-offs

- **Scope glob breadth.** `*:r` is wider than `admin/*:r`. Acceptable in a single-instance deploy; revisit if a second instance lands.
- **Migration cost.** Pre-cutover smoke data under `admin/<agent>/*` must be wiped (it's synthetic — no loss). Legacy POC data migrates with a namespace-mapping step in `deploy/migration/poc-to-v1.py`.
- **Vault path convention.** The on-disk vault structure in [[03-system-design/namespaces]] previously partitioned curated files under `vault/curated/<tenant>/`. Under agent-as-tenant this becomes `vault/curated/<agent>/` (Alex's curated knowledge lives under `vault/curated/alex/`). Documented in the v1.0 spec refresh.

## Migration

Executed as part of the v1.0 cutover:

> **Note (2026-10-01):** `deploy/migration/poc-to-v1.py` has since been removed from the repository; the references to it in this ADR are historical.

1. Wipe canonical Qdrant (synthetic + smoke rows).
2. Re-mint every bearer token under the new convention.
3. Update `AgentConfig` in `openclaw-livekit` (`musubi_v2_namespace = "alex/voice"`, etc.).
4. Update openclaw plugin tokens + presence defaults.
5. Run `deploy/migration/poc-to-v1.py` with namespace mapping `legacy payload.agent → <agent>/voice/episodic`.
6. Deploy livekit agents against the clean canonical instance.
7. Cut v1.0.0.

## Related

- [[03-system-design/namespaces]] — updated to reflect the new convention.
- [[10-security/auth]] — scope examples updated.
- [[07-interfaces/canonical-api]] / [[07-interfaces/sdk]] / [[07-interfaces/mcp-adapter]] — example namespaces refreshed.
- ADR [[13-decisions/0028-retrieve-2seg-namespace-crossplane]] — 2-seg retrieve still works unchanged; the tenant slot just contains an agent name now.
