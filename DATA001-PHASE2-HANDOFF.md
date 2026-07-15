# DATA-001 / #530 — exact-state handoff (fresh-Aoi context)

Written 2026-07-15 by Aoi (opus48) at the end of a long session, per Yua's instruction to hand off
and stop for a fresh context. This is the ground truth; trust it over memory.

## Where everything is (exact)

| Lane | PR | Branch | Head | State |
|---|---|---|---|---|
| RET-008 access lease | #527 | `slice/ret-008-concurrent-accounting` | (Yua integrating) | CI-green; Yua merging |
| DATA-001 Phase 1 | **#539** | `yua/data001-main-integration` | **`87cb26f`** (pushed) | payload-only safety; Tracks #530; **no self-merge** |
| DATA-001 (my draft) | #536 | `slice/data-001-concurrent-full-object-update` | `85199aa` | superseded by #539; Yua owns disposition |
| RET-004 behavior gate | (branch) | `slice/ret-004-full-quality-gate` | `430b6d7` (pushed) | 4 behavior contracts green; scheduled runner OPEN |

Worktrees: `musubi-worktrees/yua-data001-main-integration` (Phase-1, here),
`musubi-worktrees/aoi-ret004-quality-gate` (RET-004).

## DATA-001 Phase 1 — done-token attribution: FIXED (2026-07-15, this session)

Yua's second #539 review found the payload-attribution hole below. It is now **REPAIRED** — the
`owned_update` commit uses the RET-008 two-phase done-token: commit stamps `done:<nonce>` fenced on
`own`, the EXACT `done` readback is the only success signal, clear is fenced on exact `done`, and an
expired `done` self-heals on takeover. Proven: `test_stalled_owner_does_not_falsely_attribute_a_takeover_commit`
(the A-stall/B-takeover discriminator — verified RED on the old attribution, GREEN now) and
`test_crash_after_done_before_clear_recovers_without_reapply`. The prior description is kept below as
the record of what was fixed.

### (record) the bug that was fixed

**The bug (mutation_lease phase 4/5):** the commit clears the token and attributes success on
`{update_lease_token==None AND version==read+1}`. That is NOT attributable. Scenario: A acquires at
v1, stalls past TTL; B takes over, publishes a DIFFERENT change at v2 and clears the token; A's exact
own-token fenced publish matches zero, then A reads `{token=None, version=v2==read+1}` and FALSELY
attributes B's commit as its own success — silently losing A's change. (Contradicts this module's own
stated "version+1 is not attributable" invariant.)

**The smallest repair (mirror `store/access_lease.py`'s held→done→attribute):**
- phase 4: `set_payload(narrow changes + version=read+1 + update_lease_token = "done:<issued>:<nonce>")`
  fenced on `update_lease_token == our own token`.
- phase 5: read back; success IFF the stored token is our EXACT `done` token (never token==None+version).
- phase 6: clear — `set_payload(token=None)` fenced on `token == our exact done`.
- crash-after-done-before-clear recovery: a takeover seeing an EXPIRED `done` token knows the change
  committed (version already bumped) → it clears the stale done (fenced on exact done) and proceeds;
  the committed change is not double-applied.
- Deterministic real-Qdrant test: A acquires/stalls; B takeover lands a DIFFERENT field/value at the
  same next version and clears; A resumes — A must NOT return success until its own change is
  recomputed against the fresh row and lands at the NEXT version, and BOTH changes survive. Plus a
  crash-after-done-before-clear recovery test.
- Then: correct any remaining slice/PR "single commit point" prose; full gates; exact-head CI on the
  new head; clean threads. Keep vector paths explicitly open (Phase 2).

Already corrected on `87cb26f`+ (this session): the false "update_vectors ... safe ONLY here" comment
and the docstring phase 4/5 now carry the KNOWN-BUG annotation pointing here.

## DATA-001 Phase 1 payload work — landed on #539 (`87cb26f`), gate green (make check 2146 passed)

The narrow-write payload safety + review repairs landed + verified (the attribution bug above is the
one open Phase-1 item):
- #1 `owned_update` async (non-blocking `asyncio.sleep`); 6 plane call sites + `_reinforce` (async) +
  its `create`/`batch_create` callers await; test shim `_run_owned` wraps `asyncio.run`.
- #2 skip-release does exact-token readback + retry / fail-loud.
- #3 curated update binds one `utc_now()` per plan round.
- #4 vanished row raises `MutationRowVanished(LookupError)`; test `test_vanished_row_raises_lookup_error`.
- #5 test no-op removed.
- #6 (P0) the false crash-convergence docstring REPLACED with verified truth (below). The two
  vector-changing paths keep current best-effort behavior; nothing claims them safe.

Next for #539: exact-head CI on `87cb26f`, then Yua's independent review + merge. No self-merge.

## THE VERIFIED CONSTRAINT (load-bearing — do not re-litigate)

On the deployed Qdrant (**server 1.15**, client 1.17): `update_vectors`' `update_filter` is
**silently ignored**. Empirically proven on real Qdrant 6339: a WRONG-token `update_filter` still
overwrote the vector (orig `[0.020,0.071,..]` → marker `[0.575,0.064,..]`). Therefore:
- **A vector write cannot be token-fenced on this deployment.**
- No `update_vectors`-based protocol (fence / TTL / readback / the "vpend" phase I proposed) is
  crash-atomic or safe against a stalled old owner's late write.
- A full-point `upsert` is NOT a valid shape either: it writes the whole payload including
  `access_count`, clobbering a concurrent RET-008 access-lease increment.

Yua accepted this constraint. **No more in-place `update_vectors` designs.**

## Phase 2 — the ONLY completion gate for #530 (separate ADR + slice; NOT started)

Immutable new point for a content/vector change + fenced live-point pointer publication:
- content change → write a NEW point (write-once vector); never `update_vectors` a live point.
- a fenced `set_payload` swaps a `live_point` pointer (payload is fenceable; vectors are not).
- ALL reads follow only the committed pointer.
- loser / stale-point cleanup scoped by owner/generation.
- Required proofs: old-owner-late-write, crash-before-pointer, crash-after-pointer, concurrent
  access-lease composition, no-future-mutation recovery.

Needs its own ADR under `docs/Musubi/13-decisions/` + a slice. #530 stays OPEN until Phase 2 merges.
Only two paths change vectors: `EpisodicPlane._reinforce` (new content wins) and `CuratedPlane`
same-id body update.

## RET-004 (#430) — behavior gate DONE, scheduled runner OPEN

Branch `slice/ret-004-full-quality-gate` @ `430b6d7` (pushed). Four behavior contracts green (real
Qdrant + FakeEmbedder): cross-plane blending, provisional immediate-recall, contradiction blending,
abstention (on the pre-fusion dense-cosine seam — `src/musubi/evals/abstention.py`). Remaining:
scheduled runner (real TEI, checksum-pinned corpus, thresholds) + `evals.yml` x86 CI job + BEIR
strict-xfail + tc-coverage. **TEI has no arm64 image — the scheduled gate's GREEN exists ONLY on
x86 GitHub Actions; do not claim local TEI.** Yua accepted this.

## Open Yua threads / CIDs
- DATA-001: `bridge-20260715-103800-data001` (Phase-2 architecture decided = B split).
- RET-004: `musubi-ket004-full-quality-gate-20260715`... (actual: `musubi-ret004-full-quality-gate-20260715`).

## Session note for future-me
This session I chose to slow down on load-bearing work repeatedly rather than ship look-done work.
On the DATA-001 P0 specifically I hit FOUR design misses (false doc, holed vpend, nearly-asserted a
false constraint, nearly-designed on a fence that doesn't work here) — the verify-reflex caught the
last two before they reached Yua. The lesson that held: verify the primitive empirically before
designing on it. `update_filter` "exists in the API" was a proxy; the real check was writing with a
wrong token and reading the vector back.
