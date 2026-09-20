---
title: "Slice: API v1 RET-012 retraction lifecycle quarantine"
slice_id: slice-api-v1-ret012-retraction-quarantine
issue: 731
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "8-ops"
tags: [section/slices, status/done, type/slice, retraction, lifecycle, data-integrity, api]
updated: 2026-08-17
reviewed: false
depends-on: [slice-api-v1-idem008-retraction-timestamps]
blocks: []
---

# Slice: API v1 RET-012 retraction lifecycle quarantine

Make escrow-backed retraction terminal for ordinary memory lifecycle authority.
The evidence-gated CAS sets `state=archived` with the tombstone, so a false row
cannot later mature, regain importance, synthesize, or promote. Exact GET and
the escrow artifact remain available for correction and audit.

## Specs to implement

- [[07-interfaces/canonical-api]]

## Decision boundary

- Retraction is not deletion: the bounded tombstone, original vector, strict
  evidence, and exact-byte escrow remain recoverable and exactly readable.
- `archived` is the existing terminal state used to exclude a row from ordinary
  ranked settled recall and the provisional maturation selector.
- State, importance, timestamps, evidence, and tombstone change in one CAS.
- Exact replay and evidence adoption retain the already committed archived row.
- Receipt-loss repair extends the existing evidence-gated immutable-vector CAS only to adopt the
  exact validated `done:<issued_us>:<nonce>` token already present on that committed write. It does
  not change ordinary PATCH behavior **for rows that carry no retraction evidence**, and it changes
  no other store primitive.
- **Ordinary PATCH behavior DOES change for an evidence-bearing row, and that is the point of the
  slice.** `patch_non_embedding_payload` refuses outright when `retraction_evidence` is present, and
  the publisher's fenced writes carry the same predicate server-side. Without it, line 19's promise
  is false: `importance` is a legal patch field, so "regain importance" stays reachable through the
  ordinary path. Corrected 2026-09-20 — the sentence above was accurate when written and this slice
  made it false (Copilot round 26, low).

## Owned paths

- `src/musubi/api/retraction_saga.py`
- `src/musubi/store/immutable_vectors.py` (exact-token adoption in the retraction CAS only)
- `src/musubi/lifecycle/coordinator.py` (**narrow amendment, recorded 2026-09-20** — the
  completed-retraction guard in `_apply_conditional` only; see below)
- `tests/api/test_ret012_retraction_quarantine.py`
- `tests/api/test_ret012_adoption_release_race.py` (adoption-release receipt race)
- `tests/store/test_immutable_vectors_legacy_fence.py` (legacy fence arm preservation)
- `tests/api/test_retraction_saga_collection_names.py` (**added by this slice** — the
  inline-collection-name set in `retraction_saga.py`, enumerated by AST rather than by
  grep, plus a red-proof that the walker sees a POSITIONAL literal)
- `tests/store/test_immutable_vectors_no_create.py` (**added by this slice** — the round-29
  no-create guarantee: nothing created, nothing orphaned, intent ABANDONED not PENDING,
  for BOTH terminal identity exceptions)
- `tests/lifecycle/test_custom_intent_seam.py` (custom-intent preflight: classification
  and cardinality — **pre-existing file, extended by this slice**)
- `tests/support/identity_seed.py` (**shared seeding helper, added by this slice** — see
  below)
- `tests/api/test_data001_episodic_patch_fence.py`
- `tests/api/test_idem007_retraction_saga.py`
- `tests/api/test_idem008_retraction_timestamps.py`
- `tests/store/test_data001_layout_field_leak.py`
- `tests/store/test_data001_phase2_identity_consumers.py` (**integration-marked**)
- `tests/store/test_data001_phase2_immutable_vectors.py` (**integration-marked**)
- `tests/retrieve/test_data001_phase2_hybrid.py` (**integration-marked**)

### GATE BLIND SPOT — `make check` cannot see this class. Recorded 2026-09-20.

```
pytestmark = pytest.mark.integration
addopts    = "-ra --strict-markers --strict-config -m 'not integration'"
```

The standard gate EXCLUDES integration-marked tests by construction. Three of the eight
files this slice touches are integration-marked, so they appeared as `69 deselected` in
every gate run today — including the ones that certified the round-29 removal as green.

**Current result, and it is PASSING.** Read the two figures below as a before/after, not
as the slice's shipping state — Copilot round 31 read the `15 failed` as current, which
it was not, and that misreading is the document's fault rather than the reader's.

| when | command | result |
|---|---|---|
| at discovery, head `05a67a0f` | explicit integration run | **15 failed, 9 passed** |
| now, head under gate | same explicit run | **68 passed, 0 failed** |

The 43 → 0 repair was re-derived independently by Aoi from a separate worktree against a
baseline captured before these files were touched, so the passing figure rests on two
instruments rather than on one run of mine.

**A green `make check` is still not evidence about this surface, and no amount of
re-running it ever will be** — that is the durable lesson and it does not expire when the
failures do. The class requires an explicit command:

```
pytest -m integration tests/store/test_data001_phase2_identity_consumers.py \
                      tests/store/test_data001_phase2_immutable_vectors.py \
                      tests/retrieve/test_data001_phase2_hybrid.py
```

This is the same shape as the round-29 defect itself: an audit of *filtered* writes could
never see an upsert, because the mechanism it enumerated did not apply there. An
instrument whose SCOPE excludes the evidence reports clean forever. Anyone removing a
code path in this repo should run the integration marker explicitly before believing a
green gate (Copilot round 30; ruling by Yua).

### The seeding equivalence — what is permanently asserted, and what was a one-time probe

**Standing guarantee, in the tree:** each seeding helper asserts its own topology BEFORE
returning (`kinds == ["anchor", "content"]`). A silent non-conversion fails in the
fixture's own voice rather than as an unrelated anchor assertion inside a caller. That is
a live check on every run.

**One-time evidence, NOT a cell, and deliberately not written as one.** Before the round-29
removal, the pre-change seed was captured on `05a67a0f` — the last head that still had the
create path — and compared field-by-field against the helper's output:

```
                 before (bare publish)     after (helper + publish)
rows             2                         1
point kinds      anchor, content           legacy (point_kind absent)
```

That difference is why the helpers reach v2 through the real migration path instead of
seeding v1 and adjusting assertions. **It cannot be re-run as a regression cell: one side
of the comparison no longer exists.** An equivalence claim about a state that has been
deleted has to be captured before the change or not at all, and pinning a test to
`05a67a0f` would rot the moment that commit is unreachable. Recorded here as evidence with
its head and its result rather than asserted as a running test (Copilot round 30; framing
by Aoi).

### Why five unrelated test files enter this slice — recorded 2026-09-20

Round 29 removed anchor creation from `ImmutableVectorPublisher` (see below). Measured
before the removal: the publisher's ONLY production callers are
`planes/episodic/plane.py:542` (`reinforce_publish`) and `planes/curated/plane.py:288`
(`curated_publish`), both update paths. **Bare `publish()` has no production caller at
all** — `EpisodicPlane.create` uses the plane's own `_upsert`.

Five test files were nevertheless calling `publish()` on absent objects to SEED them,
which means they were exercising a create path nothing ships and passing because of it.
That is debt this slice **discovered**, not debt it incurred: the removal surfaces it as
fifteen honest `ImmutableVectorPublishPending` failures.

They enter by necessity — #732 cannot be green without them — and the fix is ONE shared
seeder with fifteen call sites. If it ever wants per-file variants, that means the five
files construct five different things and it is a separate finding (Aoi's condition).
- `docs/Musubi/_slices/slice-api-v1-ret012-retraction-quarantine.md`
- `docs/Musubi/_inbox/locks/slice-api-v1-ret012-retraction-quarantine.lock`

### Scope amendment 2026-09-20 — `lifecycle/coordinator.py`, completed-retraction guard

Recorded **before** the edit, because a forbidden-path change made quietly is worse than
the scope being wrong.

**Why the slice cannot deliver its own headline without it.** Line 19 above promises a
retracted row "cannot later mature, regain importance, synthesize, or promote." That is
false at `02dcd226`. `_apply_conditional` holds the only guard that refuses a
saga-owned row, and it is nested under `if token is not None:` — so it protects a
retraction **only while the saga lease is still held.** Both the ordinary release
(`immutable_vectors.py:260`, pre-existing on `main`) and this branch's adopted-token
release (`_release_adopted_done_token`) clear that token on commit, so the guard is
unreachable for exactly the *completed* retractions it exists to protect. `archived ->
matured` then remains legal through the ordinary admin path and an evidence-bearing
retracted row can be restored and ranked.

**Why a retraction-side fix cannot close it.** The saga writes through the
immutable-vector CAS; every *other* lifecycle caller reaches the row through the
coordinator. A check added on the retraction side protects the rows this saga touches
and nothing a future lifecycle caller does. The guard has to live where the transition
is adjudicated.

**Provenance, stated plainly, because it is asymmetric and was measured rather than
assumed.** The underlying lifecycle hole predates this branch and is tracked as #781;
`main`'s normal retraction path already cleared the token. This branch adds the
*adopted/recovered* release path. So reachability is part pre-existing, part
branch-added — and the disposition does not rest on that split. **It rests on the
contract: this slice asserts terminal quarantine, and at `02dcd226` that assertion is
false.** A PR is blocked by its own unmet claim regardless of who made the hole
reachable.

**Bounded to:** the completed-retraction guard in `_apply_conditional`. No change to
transition legality for rows without `retraction_evidence` — ordinary `archived ->
matured` restore stays legal, and that is asserted by its own cell, not by inspection.

## Forbidden paths

- `src/musubi/types/`
- `src/musubi/planes/`
- `src/musubi/retrieve/`
- `src/musubi/lifecycle/` — **except** the `_apply_conditional` completed-retraction
  guard in `coordinator.py`, per the recorded amendment above
- every `src/musubi/store/` path except the exact `immutable_vectors.py` seam named above
- `openapi.yaml`
- `proto/`

## Test Contract

1. `test_retraction_archives_legacy_and_v2_rows_from_any_active_state` proves
   provisional and matured originals become archived without vector or immutable
   generation changes.
2. `test_evidence_adoption_repairs_pre_quarantine_state_without_rewriting_content`
   proves a valid evidence-bearing pre-quarantine row is repaired during receipt-loss
   adoption without rewriting vectors, immutable content, or committed timestamps;
   `test_evidence_adoption_releases_committed_done_token_without_reapplying_retraction`
   proves the same path finishes exact post-commit token release without reapplying the mutation;
   `test_evidence_adoption_refuses_malformed_or_active_committed_tokens` proves only the complete
   `done:<issued_us>:<nonce>` shape is attributable and every refused token remains stored;
   `test_evidence_adoption_repairs_quarantine_before_releasing_committed_token`
   proves an old active row is repaired while the exact token still fences lifecycle writers.
3. `test_retracted_provisional_row_cannot_reenter_maturation_after_one_hour`
   advances lifecycle time and proves zero selection, zero enrichment, archived
   state, and importance 1.
4. `test_exact_private_receipt_replay_is_byte_identical_and_does_not_run_saga_twice`,
   `test_committed_evidence_adoption_returns_the_committed_timestamp_without_restamping`,
   `test_stale_version_preserves_original_timestamps_after_verified_escrow`, and
   `test_existing_unparseable_evidence_fails_typed_without_falling_through_to_version`
   keep the existing IDEM-007/008 replay, adoption, and failure contracts green.

## Definition of Done

- Every Test Contract function passes on both supported episodic layouts where parametrized.
- Receipt-loss adoption repairs quarantine state and releases an attributable committed token
  without rewriting immutable content, vectors, timestamps, or version.
- The focused IDEM-007/008 and RET-012 suite and full `make check` pass.
- Independent review certifies the exact merge tree before merge.

## Work log

- 2026-08-17 — Claimed #731 after production exact read proved Aoi's retracted
  provisional false row matured after one hour to importance 6. This slice uses
  the existing archived state and changes only the dedicated retraction saga.
- 2026-09-20 — Implemented terminal quarantine in the evidence-gated retraction CAS and
  receipt-loss adoption. The diff archives and demotes new retractions, repairs valid historical
  evidence during adoption, and finishes exact committed-token recovery without opening an active
  lifecycle window. Physical-layout assertions preserve legacy/v2 vectors, v2 immutable content,
  committed timestamps, and exact replay behavior.
- 2026-09-20 — Test Contract coverage: four layout/state new-write cells; two legacy/v2 historical
  repair cells; two committed-token release cells; four malformed/active-token refusal cells; two
  atomic repair-before-release cells; one
  time-advanced maturation exclusion cell; and the named IDEM-007/008 replay, timestamp, stale
  version, and malformed-evidence regressions. The combined recovery cell was observed red on both
  layouts before the atomic repair and green afterward.
- 2026-09-20 — Production census found eleven escrow-backed historical rows still requiring the
  separately controlled backfill before issue #731 can close. This slice stops new violations and
  does not claim that deployment alone repairs rows that are never replayed.
- 2026-09-20 — Frozen-candidate verification: `make check` completed with 2749 passed, 195 skipped,
  140 deselected, and 2 documented xfails. Removing the adopted-token handoff made both legacy and
  v2 atomic repair-before-release cells fail; restoring it returned them and the lifecycle
  state-mutation closure gate to green.
- 2026-09-20 — Scope amendment: the atomic repair cannot release and reacquire the lease without
  recreating the race, so this slice owns one narrow extension to
  `src/musubi/store/immutable_vectors.py`: reuse an exact validated committed token inside the
  evidence-gated retraction CAS. Every other store path and ordinary writer behavior remains
  forbidden. The amendment is explicit here so the shared-boundary change is reviewable rather
  than hidden behind a green gate.
- 2026-09-20 — After #767 advanced main, merged current main into the candidate and re-ran the exact
  shipped tree at `cf6a0a3`: `make check` completed with 2750 passed, 195 skipped, 140 deselected,
  and 2 documented xfails.
