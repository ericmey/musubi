---
title: "ADR 0024: Kong integration deferred for Musubi v1"
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

# ADR 0024: Kong integration deferred for Musubi v1

**Status:** accepted
**Date:** 2026-04-20
**Deciders:** Eric

## Context

[[13-decisions/0014-kong-over-caddy]] established Kong as the gateway of record
for Musubi's external API surface. The ADR's §Decision prescribes that Musubi
Core binds HTTP on `<musubi-ip>:8100`, Kong fronts it with TLS + OAuth + rate
limits, and clients reach it at `https://<musubi-host>/v1/*` where DNS
resolves `<musubi-host>` to the Kong VM.

Since that ADR was accepted, additional operator context surfaced:

- Kong on `<kong-gateway>` only routes `*.<external-domain>` traffic (the
  homelab's public-ish domain). It does not serve the internal
  `*.<homelab-domain>` namespace where `<musubi-host>` actually lives.
- `<musubi-host>` currently resolves to `<musubi-ip>` directly via the
  gateway's internal DNS resolver. There is no Kong route and no DNS pointer
  to the Kong VM for Musubi.
- Musubi has not been given an `<external-domain>` address. The product is
  VLAN-internal today; no external clients exist.
- Musubi Core ships its own JWT/OAuth 2.1 validation (via
  [[_slices/slice-auth|slice-auth]]) and in-app rate-limiting
  (via [[_slices/slice-ops-hardening-suite|slice-ops-hardening-suite]]).
  The edge policies Kong was expected to provide are already enforced
  in-process.

So ADR 0014 describes a future-state where Musubi is externally reachable
through Kong. That state is not yet in place — and the runbook was treating
the Kong step as a first-deploy gate when it is not.

## Decision

- **Kong integration is deferred for Musubi v1.** The first deploy brings
  Musubi up VLAN-internal at `<musubi-host>:8100` (plain HTTP) with no Kong
  upstream. Clients are trusted agents on the same VLAN; in-app JWT validation
  and rate-limit are the enforcement points.
- [`deploy/kong/musubi-prod.yml`](../../../deploy/kong/musubi-prod.yml) remains in the
  repo as a **staged** declarative config. It is NOT applied during first
  deploy. It is the ready-to-apply artifact for the future externalisation
  step.
- [first-deploy runbook](../../../deploy/runbooks/first-deploy.md) steps 6 and 7 are
  marked OPTIONAL with an applicability check: if the operator context shows
  Musubi is Kong-fronted, run them; otherwise skip to step 8. Both branches
  are explicitly covered.
- ADR 0014 remains `accepted` as the forward target. This ADR does not
  supersede it; it defers the implementation until the trigger events below.

## Re-enablement triggers

Reopen the Kong integration step when ANY of:

1. Musubi is given an `<external-domain>` DNS record (`musubi.<external-domain>`).
2. An external client (outside the VLAN) needs access — LiveKit worker on a
   cloud VM, remote agent, third-party integration, etc.
3. Homelab internal DNS changes to route `<homelab-domain>` traffic through
   Kong (would require Kong to serve the internal namespace, not just the
   external one).
4. TLS termination becomes required on the internal surface (e.g., a policy
   that demands encrypted transport even on trusted VLANs).

When any trigger fires: write a follow-up ADR that supersedes the "deferred"
status here, run [`deploy/kong/musubi-prod.yml`](../../../deploy/kong/musubi-prod.yml)
through `deck gateway sync`, update internal DNS, and re-run steps 6 and 7 of
the first-deploy runbook (now mandatory for that deploy).

## Consequences

### Positive

- **First deploy is simpler.** One fewer dependency (Kong route config), one
  fewer failure surface. The runbook goes straight from systemd units to
  smoke verify.
- **Fewer moving parts to understand.** VLAN-internal Musubi + in-app auth is
  a shorter causal chain than Kong → route → OAuth plugin → Musubi than a
  first-time operator needs to reason about under pressure.
- **No divergence between spec and reality.** ADR 0014 described the target;
  this ADR pins the present. Operators can tell which of the two is load-bearing
  for the deploy in front of them.

### Negative

- **ADR 0014 is no longer fully realised.** A reader of that ADR alone would
  expect Kong integration to be complete. They must follow through to this
  ADR to see the deferral. Mitigation: the `deploy/kong/*.yml` file's header
  comment points at this ADR; the runbook steps 6–7 point at this ADR; the
  crosslink back from 0014 is a follow-up edit captured below.
- **Accumulated technical debt until externalisation happens.** If Musubi
  stays internal-only indefinitely, the staged Kong config rots. Mitigation:
  retire the staged file via a supersession ADR if the externalisation
  triggers are formally dropped.

### Neutral

- [[_slices/slice-auth]] and [[_slices/slice-ops-hardening-suite]] remain the
  enforcement points. Their behaviour is unchanged by this ADR.

## Alternatives considered

### A. Fully implement Kong integration before first deploy

- Why considered: the cleanest alignment with ADR 0014's §Decision.
- Why rejected: requires giving Musubi an `<external-domain>` DNS record,
  issuing a TLS cert, and writing internal Kong policy for it — all of which
  are scope-creep relative to the current goal (get Musubi running on the
  host). Blocking the first deploy on this is a pessimistic scheduling
  choice for a feature no current client needs.

### B. Retire ADR 0014 as misapplied

- Why considered: if Musubi stays VLAN-only, Kong is never needed.
- Why rejected: the original ADR reasoning (one gateway control plane across
  the homelab) is still sound as a future direction. The right move is to
  defer the implementation, not invalidate the decision. Retirement would be
  premature given no signal that Musubi stays internal-forever.

### C. Put Caddy back on the Musubi host as an interim gateway

- Why considered: provides local TLS termination without depending on Kong.
- Why rejected: contradicts 0014 on principle, duplicates effort that Kong
  will eventually own, and solves a problem that doesn't exist on a trusted
  VLAN. Only worth revisiting if Musubi is exposed externally but Kong still
  doesn't route it — a narrow fallback.

## Cross-slice updates landing in the same PR

- [first-deploy runbook](../../../deploy/runbooks/first-deploy.md) steps 6 and 7
  marked optional with applicability guards (updated in this change).
- `.agent-context.local.md` § *Homelab topology* already captures the reality
  this ADR codifies (added 2026-04-20 during reconnaissance).

## Follow-ups (not in this PR)

- Append a "Status update 2026-04-20: implementation deferred — see ADR 0024"
  stanza to [[13-decisions/0014-kong-over-caddy]] so a reader starting from
  0014 is redirected here. (A two-line edit; held for a follow-up PR to keep
  this change focused.)

## References

- [[13-decisions/0014-kong-over-caddy]] — the ADR this one defers.
- [[_slices/slice-auth]] — in-process auth, the policy that makes this
  deferral safe.
- [[_slices/slice-ops-hardening-suite]] — in-app rate limiting.
- [first-deploy runbook](../../../deploy/runbooks/first-deploy.md) — the procedure
  updated to reflect this decision.
- `.agent-context.local.md` → *Homelab topology* (gitignored) — operator
  context capturing `<homelab-domain>` vs `<external-domain>` split.
