# DATA-001 Phase 2 — identity-consumer inventory (#530)

Work-log companion to [`data001-phase2-immutable-vectors.md`](./data001-phase2-immutable-vectors.md).
The multi-point layout (v2 ANCHOR + write-once CONTENT points, or a v1 legacy row) changes what
*every* consumer of a `(namespace, object_id)` in the `musubi_episodic` / `musubi_curated`
collections sees. This table is the grep-backed sweep of those seams and the behavioral fix each
needs. Concept / thought / artifact planes have no anchors and are out of scope.

## Key discriminator (why a seam breaks or not)

A v2 **CONTENT** point carries only `object_id, namespace, point_kind="content", generation,
owner_token, content, summary, [title]` — **no** `state, version, *_epoch, importance, access_count,
reinforcement_count, vault_path, supersedes/superseded_by`. A v2 **ANCHOR** carries the full mutable
payload **plus** `point_kind="anchor", live_point, pointer_version, committed_operation_id,
vector_layout_version` and a **zero vector** (brand-new) or a **stale legacy vector** (converted
in place). A **v1** legacy row = full payload, no `point_kind`, real vector. Fresh rows are v1 until
a vector-changing reinforce/update converts them.

Consequences:

- A filter on a discriminating field (`state`, `*_epoch`, `importance`, `vault_path`, …) **auto-excludes**
  content shells — safe *iff* the consumer reads raw `.get()`; **breaks if it `model_validate`s** (the
  anchor carries extra Phase-2 keys and the models are `extra="forbid"`).
- A filter on **only `namespace`/`object_id`** matches BOTH anchor and content → double-count /
  arbitrary-point / `model_validate` blow-up.
- **State-filtered VECTOR queries silently drop the real vectors** — the meaningful dense/sparse
  vectors live on content points, which lack `state`; the state `must` excludes them and leaves the
  zero/stale-vector anchor. This is the central retrieval-correctness break, not just a validate nuisance.

## Two cross-cutting rules for the fixes

1. **Two anchor-id spaces.** A converted-in-place object keeps its legacy id
   (`uuid5(_POINT_NS, object_id)`); a brand-new object's anchor is at `anchor_point_id =
   uuid5(_ID_NS, …)`. Any id-addressed read/delete is wrong for at least one origin — **prefer
   payload-filtered anchor resolution over id derivation.**
2. **Resolve, then validate.** Never `model_validate` a scrolled/queried payload directly from these
   collections — resolve the authoritative identity (anchor-over-content, or v1) first.

## MUST FIX

| # | seam (file:line, func) | op | risk | fix | status |
|---|---|---|---|---|---|
| 1 | `lifecycle/coordinator.py:872 _read_object` (→ `_persist_event:902`, `_apply_conditional/_confirm:1005`, `_cur:1371`) | scroll ns+oid limit=2 | anchor+content → count=2 → every episodic/curated transition fences/abandons | exclude `point_kind=content` (no-op for v1 + concept/thought/artifact) | **DONE + proven** (test 24, red-proofed) |
| 2 | `store/raw_lookup.py:71 raw_payload` | scroll ns+oid limit=1 | returns arbitrary point (may be content shell) | target identity (must_not content; resolve anchor-over-content) | TODO |
| 3 | `store/raw_lookup.py:101 retrieve_by_point_id` (callers pass legacy `_point_id`) | retrieve by id | brand-new anchor at a different id → None (delete 404); converted anchor single-delete orphans content | address full layout, not one derived id | TODO |
| 4 | `planes/episodic/plane.py:559 get()` (→113 `_memory_from_payload`→validate) | scroll ns+oid limit=1 | validates an anchor/content shell → raises; cascades to patch/transition/reinstate | resolve committed content before validate | TODO |
| 5 | `planes/episodic/plane.py:491 _find_dedup_candidate` | query dense, ns filter | ranks content + stale converted anchors; returns shell → validate fail / stale candidate | query content, exclude anchors, resolve via anchor, overfetch+underfill | TODO |
| 6 | `planes/episodic/plane.py:624 query()` | query dense, ns+state | content excluded by state; anchors (zero/stale vec) surface → validate fail + real vectors unreachable | anchor-aware retrieval | TODO |
| 7 | `planes/episodic/plane.py:831 delete()` (via `retrieve_by_point_id`) | delete single `_point_id` | misses relocated anchor; orphans content | delete full layout: anchor/v1 + every content point | TODO |
| 8 | `planes/curated/plane.py:321 _find_by_vault_path` (→validate) | scroll ns+vault_path | anchor carries vault_path → returns anchor → validate fail on extra keys; cascades to create dedup | resolve+validate via anchor | TODO |
| 9 | `planes/curated/plane.py:395 find_by_vault_path` | scroll vault_path limit=2 | anchor payload → validate fail | resolve+validate via anchor | TODO |
| 10 | `planes/curated/plane.py:476 get()` | scroll ns+oid limit=1 | shell → validate fail; cascades to transition | resolve before validate | TODO |
| 11 | `planes/curated/plane.py:522 query()` | query dense, ns+state+bitemporal | same break as episodic query | anchor-aware retrieval | TODO |
| 12 | `planes/curated/plane.py:575 scan_vault_rows` | scroll ALL, no filter | iterates content shells + anchors → validate fail / `raise ValueError` on content | scroll identity rows only; resolve via anchor | TODO |
| 13 | `retrieve/hybrid.py:439 _query_points` (filter 425, hits 465) | prefetch+fusion, ns+state | content excluded by state; anchors surface; hits carry extra keys | anchor-aware hybrid: rank content, resolve via anchor, post-hydration state/validity filter, bounded overfetch+underfill | TODO |
| 14 | `lifecycle/synthesis.py:424 scroll + 446 validate; 467 retrieve(episodic_point_id) + 484 validate` | scroll+retrieve-by-id | anchors → validate fail; retrieve-by-legacy-id misses brand-new anchors | resolve per candidate; address anchor id | TODO |
| 15 | `api/routers/_scroll.py:71 scroll_namespace` (→ episodic/curated list validate) | scroll ns only | anchors+shells → validate 500s; page counts doubled | filter identity rows; resolve+validate via anchor | TODO |
| 16 | `api/routers/namespaces.py:85 namespace_stats count` | count ns only | count inflated (anchor + N content per object) for episodic/curated | count identity rows only (must_not content) | TODO |
| 17 | `api/routers/writes_curated.py:168 PATCH set_payload` (Filter object_id only) | set_payload | unfenced, no ns/version/kind → writes onto content shell + anchor | fence to anchor identity (kind=anchor + ns + version) | TODO |
| 18 | `lifecycle/transitions.py:302 _locate_object / 311 _scroll_by_object_id / 336 _lookup_point_id` | scroll oid only limit=1 | arbitrary shell; admin lineage set_payload may hit content | resolve identity via anchor; return anchor/v1 id only | TODO |
| 19 | `retrieve/recent.py:154 scroll ns+state order_by epoch` | scroll | returns anchor payloads (extra keys + zero/stale vector) | resolve identity via anchor before handing out | TODO |

Also: `api/routers/writes_episodic.py:446 delete` delegates to episodic `plane.delete()` (#7) — inherits its fix.

## Already safe

- `store/mutation_lease.py` + `store/access_lease.py` — `_EXCLUDE_CONTENT` (`must_not point_kind=content`) on every read/CAS; leases hit only the identity row.
- `store/immutable_vectors.py` — the Phase-2 layer itself (anchor-filtered, fail-closed resolve, full content-fanout delete).
- `raw_lookup.py:52 point_exists` — boolean presence, still correct (imprecise, not wrong).
- `api/routers/retrieve.py:348 _expand_wildcard_targets` — reads only `namespace`, dedups into a set.
- `lifecycle/reflection.py`, `maturation.py`, `demotion.py` sweeps — discriminating-field filters exclude content and read raw dicts. **Caveat:** their *apply* rides `coordinator.transition` → depend on seam #1 (now fixed).

## N/A (concept / thought / artifact — no anchors)

`planes/concept/*`, `planes/thoughts/*`, `planes/artifact/*`, `lifecycle/promotion.py`,
`lifecycle/demotion.py` concept/artifact branches, `api/routers/contradictions.py`,
`concepts.py`, `artifacts.py`, and the concept/artifact/thought iterations of `namespace_stats`.

## Pre-existing debt surfaced (not a regression)

`tests/lifecycle/test_c6b_atomicity.py::test_r21_route_controls_final_200_and_err_typed[lifecycle|episodic]`
fail at `d07552a` (before seam #1) because `episodic.create → _reinforce` FAILS CLOSED without a wired
publisher. These flip green with the **fixture-injection / composition** work item (wire the
`ImmutableVectorPublisher` into the episodic + curated planes in API + worker compositions and test
fixtures), tracked alongside this inventory.
