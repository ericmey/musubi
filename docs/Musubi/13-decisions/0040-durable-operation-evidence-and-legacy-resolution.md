---
title: "0040: Durable Operation Evidence and Legacy Resolution"
section: 13-decisions
tags: [architecture, api, security, idempotency, type/adr, status/proposed]
type: adr
status: proposed
date: 2026-08-02
updated: 2026-08-02
deciders: [Eric, Aoi, Yua]
---

# 0040: Durable Operation Evidence and Legacy Resolution

## Context

ADR 0039 makes a completed successful response durable before Musubi releases its
bytes. That closes client-response loss after receipt commit. It deliberately does
not close the earlier crash interval between a storage mutation and receipt commit.
After that interval, an authorization-bound receipt lookup returns `absent` even
though an object may have been inserted or an existing object may have been
reinforced. Search absence cannot distinguish those outcomes and must never
authorize replay.

Issue #603 is concrete production evidence. Three Yua verified-delivery rows crossed
the client's durable `post_attempted` boundary without a completed receipt. Exact
inspection separated them into two classes:

- Two episodic requests contain 55,327 and 36,367 UTF-8 content bytes. Every
  deployable Musubi version, from the oldest repository tag through v1.18.2, runs a
  32,768-byte guard as the first executable statement in
  `EpisodicPlane.create`. They therefore cannot have mutated storage whether or not
  their POST reached Musubi.
- One 680-byte episodic request is genuinely legacy-ambiguous. The historical
  mutation stored no authorization- and digest-bound operation evidence with an
  object, so a future implementation cannot reconstruct that evidence
  retroactively.

The legacy runtime boundary was exercised on v1.18.2 with exactly 32,769 ASCII content
bytes. Before and after an exact durable-mode request, its authorization-bound
receipt lookup was `absent` and the namespace contained 1,511 episodic identities;
the request returned HTTP 500. This is one-sided boundary evidence: 32,769 was
rejected, while exactly 32,768 was not exercised. IDEM-005 subsequently fixed the
create and batch-create wire contract: exactly 32,768 UTF-8 content bytes proceeds,
while 32,769 returns HTTP 422 with `CONTENT_TOO_LARGE` after authorization and
before idempotency or plane execution. The code, never 422 alone, is the terminal
non-mutation signal. This contract does not cover PATCH content replacement,
including retraction; that pre-existing path is intentionally tracked as a
separate policy decision in Issue #611.

The current receipt lookup requires namespace write authority. Its `absent` result
is scoped to the authenticated principal and authorized namespace; it does not mean
fleet-global absence. A read-only second safety seat cannot independently perform
that lookup today. This is a security and auditability decision, not permission to
weaken the existing endpoint casually.

## Decision

### 1. Preserve the fail-closed client boundary

`post_attempted=1` plus a non-terminal receipt result never authorizes a second
POST. Search, recent retrieval, tag lookup, approximate matching, object counts, and
operator intuition are not delivery receipts.

The server will add durable operation evidence for receipt-eligible single-object
captures. The operation identity remains bound to authenticated issuer, subject,
presence, HTTP method, route operation id, authorized namespace, idempotency key,
and byte-exact canonical request digest. Raw idempotency keys must not be stored in
Qdrant payloads.

### 2. Reserve before execution and preserve terminal rejection

Before a receipt-eligible handler may mutate storage, Musubi durably records an
authorization- and digest-bound operation reservation. The internal operation
journal distinguishes at least:

- `reserved`: durable identity exists, but handler execution is not yet proven;
- `in_flight`: one live owner is executing the operation;
- `rejected`: a typed terminal response proves no object mutation was permitted;
- `mutated`: exact object-side evidence proves the mutation committed;
- `completed`: an exact durable response receipt exists;
- `conflict`: the same identity was presented with divergent scope or digest; and
- `orphaned`: recovery evidence is zero, multiple, inconsistent, or otherwise
  insufficient for an automatic conclusion.

`rejected` is durable operation evidence, not an ordinary transient error. It stores
the exact response proof needed for the client to terminalize the request without
claiming success.

### 3. Couple evidence to every eligible mutation shape

The server stores an opaque operation identity and request digest in the same
authoritative mutation as the exact result it proves. The implementation must cover
all receipt-eligible outcomes, not only the easiest insert path:

- fresh episodic insert;
- episodic compatible-dedup reinforcement of an existing object;
- fresh curated insert; and
- curated same-object update or dedup behavior, if eligible at implementation
  time.

The design must compose with DATA-001 immutable-vector anchors, version fences, and
`committed_operation_id`; it must not add an unfenced full-object write. A later
mutation must not erase the only evidence for an unresolved earlier operation. The
implementation may reuse the existing mutation-intent machinery, but reuse is
accepted only if exact external operation evidence survives until a durable receipt
or explicit terminal operator disposition exists.

After a crash, authorized reconciliation returns an existing object only when
principal, namespace, operation, identity, digest, and exactly one object-side
evidence record agree. Zero or multiple matches become `orphaned` and remain
fail-closed. No search result or retrieval absence participates in that decision.

### 4. Keep the journal foundation internal until evidence is actionable

The durable journal foundation may land in a separate implementation PR, but it is
internal-only. That PR must not add public `reserved`, `rejected`, `mutated`, or
`orphaned` response values; alter OpenAPI; or authorize any client behavior.

Public lookup states ship only in the integration slice that also lands
mutation-coupled evidence and exact reconciliation. This prevents a client from
misreading a richer but still unactionable state name as replay authorization.
Fleet client adoption is gated on that integration, never on the journal foundation
alone.

### 5. Treat legacy evidence as non-retroactive

New object-side evidence cannot prove what an older mutation did. Legacy rows may
reach a terminal local state only through one of exactly two resolution kinds:

```text
proven_non_mutating_rejection
operator_abandon
```

No third kind is permitted. In particular, there is no `probably_absent`,
`assumed_delivered`, or content-search-based resolution. Adding a probabilistic kind
would recreate the unsafe inference this ADR exists to remove.

The fleet resolver represents the evidence as a schema-enforced discriminated
union. Its minimum contract is:

```text
ProvenNonMutatingRejection
  resolution_kind = "proven_non_mutating_rejection"
  event_id
  namespace
  operation_id
  expected_request_digest
  content_bytes
  limit_bytes
  source_ordering_evidence
  version_coverage
  boundary_evidence
  receipt_observation

OperatorAbandon
  resolution_kind = "operator_abandon"
  delivery_state = "unknown"       # required Literal, not prose
  event_id
  namespace
  operation_id
  expected_request_digest
  reason
  receipt_observation

ReceiptObservation
  status = "absent"
  principal
  token_scopes
  observed_at
  namespace
  operation_id
  request_digest
```

The receipt observation records no token value or secret. Its principal, effective
scopes, timestamp, and exact lookup identity are required because `absent` is an
authorization-scoped observation, not fleet-global truth.

The resolver runs under one `BEGIN IMMEDIATE` transaction and requires the exact
event id and recomputed request digest. The canonical digest binds both the exact
request-body bytes and `request_content_type`; a body-only comparison is not an
equivalent gate. All three held rows reproduce their stored digests through the
currently installed canonical digest implementation. Independent negative controls
showed that either a one-bit body change or forcing `text/plain` changes the digest,
so no legacy compatibility shim is required.

The target must still be `pending`, have `post_attempted=1`, and have no `object_id`
or active lease. A row that acquires an object id before commit is refused. Applying
byte-identical evidence twice is idempotent; divergent evidence or resolution kind
is refused. Resolution preserves the original content, request bytes, content type,
hashes, and capture event. Direct SQLite surgery is not an accepted resolution path.

For `proven_non_mutating_rejection`, the current 32 KiB evidence records content
bytes, never total request-body bytes. It cites the first-executable-statement guard,
coverage from v0.3.0 through the deployed v1.18.2 source, and the one-sided runtime
boundary: 32,769 rejected; 32,768 untested.

For `operator_abandon`, `delivery_state="unknown"` is required by schema. Abandoning
the local delivery intent is a deliberate policy choice to stop retrying; it does
not claim the historical mutation was absent or present.

### 6. Preserve the receipt authorization boundary until audit policy is decided

The ordinary receipt lookup keeps namespace write authority in this ADR. A
read-scoped endpoint can otherwise become an idempotency-identity discovery surface.
Two-seat auditability requires a separately designed operator/auditor contract or a
verifiable non-secret evidence receipt; it must preserve principal and namespace
non-disclosure. Until that contract lands, receipt-absence claims are one-seat
observations by construction and must say so explicitly.

### 7. Keep the single-worker deployment gate

`WEB_CONCURRENCY=1` remains enforced. This ADR closes the external-outbox recovery
path under that deployment but does not silently claim the broader durable
multi-worker ownership contract in Issue #558. Any second API worker, horizontal
replica, or cross-process writer still reopens #558 before deployment.

## Implementation sequence

1. **Spec and decomposition:** accept this ADR, record the current API boundary,
   and create the implementation slices.
2. **Legacy fleet resolver:** add the schema-enforced two-kind resolver, exact
   digest and state gates, durable evidence fields, idempotent same-resolution
   behavior, and divergent/race refusal. Resolve only the two proven oversized
   rows first; the 680-byte row requires explicit `operator_abandon` and keeps
   `delivery_state="unknown"`.
3. **Internal server journal:** add durable reservation and state machinery with no
   public API or OpenAPI changes and no client adoption.
4. **Mutation evidence and public reconciliation:** integrate fresh and dedup paths,
   expose only actionable public states, and prove exact crash recovery.
5. **Fleet adoption and deployment:** teach the canonical harness the completed
   contract, deploy serially, run integrity sweeps and crash probes, and reconcile
   production rows through supported commands.

The intentional create and batch-create 32 KiB rejection formerly returning HTTP
500 is resolved by Issue #606. The PATCH/retraction exception is tracked in Issue
#611. The
read-only auditor's inability to inspect receipt state is tracked in Issue #607.
They remain separate defects rather than being hidden inside an idempotency
implementation PR.

## Test contract

The implementation slices collectively prove:

1. client crash after local mark but before network remains resumable without a
   blind POST;
2. server crash after reservation but before mutation resumes exactly once;
3. server crash after fresh insert but before receipt recovers the exact object;
4. server crash after compatible dedup reinforcement but before receipt recovers
   that exact existing object and does not reinforce twice;
5. curated receipt-eligible mutations have equivalent proof or are removed from
   eligibility before deployment;
6. typed terminal rejection survives response loss and process restart;
7. principal, namespace, operation, identity, or digest conflict fails closed
   without cross-boundary disclosure;
8. zero or multiple object evidence becomes a typed orphaned operator state;
9. later mutation cannot erase unresolved operation evidence;
10. replay-cache expiry, receipt-ledger restart, and API restart preserve recovery;
11. the internal journal foundation exposes no public contract before integration;
12. `operator_abandon` requires `delivery_state="unknown"` in schema;
13. resolver digest mismatch, active lease, acquired object id, divergent evidence,
    and repeated resolution behave fail-closed; and
14. the three current legacy request digests recompute exactly from their persisted
    request bytes and content types before any production resolution.

## Consequences

- Verified-delivery clients can eventually distinguish safe rejection, active
  execution, exact completed mutation, conflict, and truly unresolved evidence
  without search inference.
- The journal and Qdrant evidence design becomes a cross-store recovery protocol and
  requires explicit crash testing at every boundary.
- Existing ambiguous rows remain historical exceptions; the server fix prevents
  recurrence but does not rewrite their past.
- Read-only two-seat receipt audit remains an explicit open security-policy item
  instead of being misrepresented as available today.
- Legacy local resolution becomes supported, auditable, and evidence-preserving
  without direct database edits.

## Alternatives rejected

### Replay after receipt absence

Rejected because `absent` does not distinguish pre-network crash, pre-mutation
server crash, mutation-before-receipt crash, or authorization invisibility.

### Use semantic or exact-content search as the receipt

Rejected because absence is not proof and compatible dedup may mutate an existing
row without preserving byte-identical content.

### Publish journal states before object evidence

Rejected because a public `reserved` state without exact object reconciliation is
the current ambiguity with more confident vocabulary.

### Store only the latest operation marker and allow later overwrite

Rejected unless the implementation first proves the earlier marker is durably
receipted or explicitly resolved. Later mutations cannot erase the only orphan
evidence.

### Retroactively infer evidence for legacy rows

Rejected because new fields cannot prove which historical operation produced or
reinforced an object.

### Add a probabilistic legacy resolution kind

Rejected because “probably fine” converts uncertainty into false delivery truth.
