---
title: "Slice: IDEM-007 Option A architecture"
slice_id: slice-idem007-option-a-adr
issue: 611
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "8-ops"
tags: [section/slices, status/done, type/slice, api, idempotency, data-integrity]
updated: 2026-08-03
reviewed: true
depends-on: []
blocks: []
---

# Slice: IDEM-007 Option A architecture

Turn the owner-selected artifact-escrow direction in Issue #611 into one binding
architecture contract before any child production code begins.

## Specs to implement

- [[13-decisions/0042-escrow-backed-episodic-retraction]]

## Decision boundary

- Option A is selected. Option B / policy discard is unauthorized, including as
  a failure-path shortcut.
- Escrow saves exact current episodic `content` UTF-8 bytes into a
  stored-unindexed sibling artifact before any episodic mutation.
- The v2 content point remains untouched and truthful; only the anchor carries
  the bounded tombstone and strict retraction evidence.
- Durable completed-response receipts remain the terminal replay proof. The
  deterministic artifact address and exact readback recover the earlier
  escrow-only midpoint.
- An opaque operation-identity hash plus canonical request digest on the logical
  row recover a tombstone that committed before response/receipt durability.
- Existing inline full-original tombstones remain valid legacy rows with no
  migration.
- No child issue moves to `status:ready` until this ADR is independently reviewed
  and accepted.

## Decomposition

1. #643 — honest stored-unindexed artifact state.
2. #644 — exact-byte deterministic artifact escrow; depends on #643.
3. #645 — typed retraction evidence and narrow VAL-002 divergence exception.
4. #646 — one escrow-first endpoint saga; depends on #644 and #645.
5. `ericmey/fleet-tools#32` — `memory-data` cutover and installed-path proof;
   depends on #646 deployment.

Issue #611 remains the umbrella and ADR owner until all five lanes close.

## Owned paths

- `docs/Musubi/13-decisions/0042-escrow-backed-episodic-retraction.md`
- `docs/Musubi/13-decisions/index.md`
- `docs/Musubi/_slices/slice-idem007-option-a-adr.md`
- `docs/Musubi/_inbox/locks/slice-idem007-option-a-adr.lock`

## Review contract

1. Stored-unindexed state is truthful and structurally non-searchable.
2. Blob durability and exact readback precede artifact-head publication.
3. Deterministic recovery rejects mere existence and divergent bytes.
4. Both namespace authorizations precede every read and storage operation.
5. V2 content point/vector/generation remain immutable.
6. Typed evidence is the only allowed projection-divergence exception.
7. Both crash midpoints have distinct durable recovery markers.
8. Stale expected version is one-shot 409, never silent rebase.
9. Ordinary content PATCH gains 32,768-byte parity only after the dedicated
   retraction path exists; metadata-only PATCH on an oversized legacy row stays
   compatible.
10. Fleet closure includes installed-path parity and cold invocation.
11. Legacy inline tombstones remain valid without migration.
12. Blob publication is atomic and no-clobber; an existing divergent blob fails
    closed rather than being overwritten.
13. Referenced escrow is excluded from automatic retention purge; orphan cleanup
    requires reverse-reachability proof.
14. A corrupt deterministic escrow address fails closed but is not permanent:
    only an explicit operator hard purge may clear the exact unreferenced address
    before a fresh retry.
15. A distinct re-retraction cannot overwrite the first evidence or orphan its
    escrow; only an exact identity-and-digest replay is adoptable.
16. Escrow KSUID ordering reflects source-object creation time, never escrow age;
    retention and recency use explicit metadata.

## Work log

- 2026-08-03 — Eric selected Option A. Aoi drafted and precision-reviewed the
  decision packet; Yua recorded the ruling on #611.
- 2026-08-03 — Aoi and Yua attacked the five-lane decomposition. Two proposed
  shortcuts were refuted: adding vector-basis metadata to the immutable content
  point would describe an anomaly that is not there, and completed-response
  receipts cannot journal the escrow-before-tombstone midpoint.
- 2026-08-03 — Created #643, #644, #645, #646, and
  `ericmey/fleet-tools#32`; all production lanes remain blocked on ADR acceptance.
- 2026-08-03 — Aoi's ADR attack found one missing recovery edge. Source inspection
  confirmed the existing idempotent operator purge can clear the exact blob even
  when no artifact head was published; the ADR now binds that manual recovery,
  refuses distinct re-retraction, and records the source-timestamp KSUID ordering
  consequence.
- 2026-08-03 — Aoi independently reproduced the purge behavior and approved exact
  head `a3993bf`. ADR 0042 is accepted; the production lanes may now advance only
  according to its dependency graph.
- 2026-08-03 — Copilot caught the new ADR's null `superseded-by` value before
  merge. It is now an explicit empty list, matching the frontmatter contract.
