---
title: "Slice: Retrieve wildcard namespace segments"
slice_id: slice-api-retrieve-wildcards
section: _slices
type: slice
status: in-progress
owner: aoi-claude-opus
phase: "8 Post-1.0"
tags: [section/slices, status/in-progress, type/slice, api, retrieve, namespace]
updated: 2026-04-24
reviewed: false
depends-on: ["[[_slices/slice-api-v0-read]]"]
blocks: []
---

# Slice: Retrieve wildcard namespace segments

> Single-segment `*` wildcards in `POST /v1/retrieve` namespaces. Lets one
> agent read across her own channels in a single call without forcing
> writes off their channel-tagged 3-seg slots. Implements
> [[13-decisions/0031-retrieve-wildcard-namespace|ADR 0031]].

**Phase:** 8 Post-1.0 · **Status:** `in-progress` · **Owner:** `aoi-claude-opus`

## Why this slice exists (2026-04-24 context)

After [[13-decisions/0030-agent-as-tenant|ADR 0030]] every Nyla-channel
writes to its own 3-seg episodic namespace (`nyla/voice/episodic`,
`nyla/openclaw/episodic`, ...). v1.0 has no read shape that spans an
agent's channels. Openclaw-Nyla cannot recall a voice conversation;
voice-Nyla cannot recall an Openclaw thread. That is the platform's
foundational read pattern (*one agent, many surfaces, one memory*) and
it does not exist yet.

This slice adds that pattern as a wildcard segment match in the retrieve
namespace. Writes stay channel-tagged (per ADR 0031, wildcards are
read-only).

## Specs to implement

- [[13-decisions/0031-retrieve-wildcard-namespace]] (this slice's normative source)
- [[03-system-design/namespaces]] (spec-update: trailer to add §Wildcards subsection)
- [[07-interfaces/canonical-api]] (spec-update: trailer to extend §Retrieve namespace shapes)

## Owned paths (you MAY write here)

- `src/musubi/api/routers/retrieve.py`           — wildcard expansion logic
- `src/musubi/sdk/async_client.py`               — surface `planes` parameter (existed in API, missing on SDK)
- `src/musubi/sdk/client.py`                     — sync mirror
- `tests/api/test_retrieve_router.py`            — wildcard expansion tests
- `tests/api/test_retrieve_wildcards.py`         — new file, dedicated wildcard test suite
- `tests/sdk/test_async_client.py`               — SDK `planes` parameter test
- `openapi.yaml`                                 — extend `namespace` schema description (additive)
- `docs/Musubi/03-system-design/namespaces.md`   — add §Wildcards (spec-update trailer)
- `docs/Musubi/07-interfaces/canonical-api.md`   — extend retrieve namespace table (spec-update trailer)

## Forbidden paths (you MUST NOT write here)

- `src/musubi/types/`                            — no new types needed; namespace stays `str`
- `src/musubi/planes/`                           — write side already rejects `*` via the existing namespace regex; verify with a test, do not change
- `src/musubi/retrieve/orchestration.py`         — orchestration already iterates concrete targets; nothing to add
- `src/musubi/retrieve/{fast,deep,blended,hybrid}.py` — same; downstream of the orchestrator
- `src/musubi/lifecycle/`                        — sweeps stay channel-aware; out of scope
- `src/musubi/api/routers/writes_*.py`           — writes don't accept wildcards (per ADR), no code change needed; verify via test
- `proto/`                                       — gRPC surface unchanged in this slice
- `src/musubi/auth/`                             — scope check loops over expanded targets (existing behaviour); no auth change

## Depends on

- [[_slices/slice-api-v0-read]]                  (done — `POST /v1/retrieve` parent)

Start condition: every upstream slice `status: done`. ✓

## Unblocks

- **openclaw-musubi plugin update** — switches retrieve callsites from
  `${presence}/episodic` → `${tenant}/*/episodic`. Companion PR in the
  external plugin repo, not this slice.
- Any future "tenant-wide recall" tool in openclaw-livekit or other adapters.

## Endpoint contract (normative)

### Wildcard rules

- `*` is a single-segment wildcard. It matches any non-empty segment string.
- A pattern's segment count determines the matched namespace's segment count.
  `nyla/*/episodic` matches only 3-seg stored namespaces; `nyla/*` matches
  only 2-seg shapes (i.e., the 2-seg fanout already in ADR 0028 — wildcards
  there don't change anything because the second segment of a 2-seg pattern
  always meant "this presence" anyway; `nyla/*` is now allowed and means
  "every presence under nyla, fanned across `planes`").
- `**` is **not** introduced.
- Wildcards in writes 400 with `BAD_REQUEST` (existing namespace regex
  already covers this; this slice locks it with a positive test).

### Expansion

For each wildcard-containing target, the router enumerates concrete matches
by scrolling the relevant plane's Qdrant collection with payload-only
`namespace`, deduping, and segment-wise pattern-matching. No cache (v1).
Empty expansion returns `{"results": []}` — not 404.

### Scope

Strict per resolved target (ADR 0028). A token without `r` on any expanded
target 403s the entire request. Wildcard scopes (`nyla/*:r`, `*/*/*:r`)
are the natural pairing for wildcard reads.

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] Every Test Contract item below is a passing test in `tests/api/test_retrieve_wildcards.py` (new) or `tests/api/test_retrieve_router.py` (existing).
- [ ] Branch coverage ≥ 85% on `src/musubi/api/routers/retrieve.py`.
- [ ] `openapi.yaml` description for `namespace` mentions wildcards; diff is additive only.
- [ ] [[03-system-design/namespaces]] gains §Wildcards; `spec-update:` trailer on the feat commit.
- [ ] [[07-interfaces/canonical-api]] retrieve namespace table updated; same trailer.
- [ ] Slice frontmatter flipped `in-progress` → `in-review` → `done`.
- [ ] Issue label `status:ready` → `status:in-progress` → closed via `Closes #<n>`.
- [ ] Lock file removed.

## Test Contract

**Syntactic shape validation (`_resolve_targets`):**

1. `test_wildcard_in_tenant_segment_3seg_accepted` — `*/voice/episodic` validates.
2. `test_wildcard_in_presence_segment_3seg_accepted` — `nyla/*/episodic` validates.
3. `test_wildcard_in_plane_segment_3seg_with_planes_list_accepted` — `nyla/voice/*` + `planes=["episodic"]` validates.
4. `test_wildcard_in_plane_segment_3seg_without_planes_list_400s` — `nyla/voice/*` alone 400s (no planes to fan).
5. `test_double_segment_wildcard_3seg_accepted` — `nyla/*/*` + planes list validates.
6. `test_all_wildcard_3seg_accepted` — `*/*/*` + planes list validates.
7. `test_wildcard_in_2seg_accepted` — `nyla/*` validates (with default `planes=["episodic"]`).
8. `test_double_star_rejected` — `nyla/**/episodic` 400s (multi-segment glob not supported).
9. `test_empty_segment_with_wildcard_rejected` — `nyla//episodic` 400s as before.
10. `test_pattern_with_4_segments_rejected` — `a/b/c/d` still 400s; wildcards don't change segment-count discipline.

**Wildcard expansion (`_expand_wildcard_targets`):**

11. `test_expansion_returns_concrete_namespaces_for_wildcard_pattern` — `nyla/*/episodic` against a Qdrant with `nyla/voice/episodic` + `nyla/openclaw/episodic` rows resolves to both targets.
12. `test_expansion_filters_by_segment_count` — `nyla/*/episodic` does not match a hypothetical 2-seg `nyla/voice` row in any plane.
13. `test_expansion_segment_match_is_literal_not_substring` — pattern `n*/voice/episodic` does NOT match `nyla/voice/episodic`; `*` is a whole-segment wildcard, not a regex char.
14. `test_expansion_dedups_namespaces` — multiple Qdrant rows under the same namespace produce one target.
15. `test_expansion_returns_empty_list_when_no_match` — `nyla/*/episodic` against an empty plane returns `[]`.
16. `test_no_wildcard_passes_through_unchanged` — concrete `nyla/voice/episodic` is not scrolled, returns `[(nyla/voice/episodic, episodic)]` directly.
17. `test_expansion_runs_per_plane_in_targets_list` — pattern targeting episodic + curated scrolls each plane's collection independently.

**End-to-end retrieve behaviour:**

18. `test_retrieve_with_wildcard_returns_results_from_multiple_channels` — captures into `nyla/voice/episodic` + `nyla/openclaw/episodic`, retrieve `nyla/*/episodic`, response includes both rows with their stored namespace fields intact.
19. `test_retrieve_with_wildcard_no_matches_returns_empty_results_not_404` — `nyla/*/episodic` against an empty Qdrant returns `200` with `results: []`.
20. `test_retrieve_with_wildcard_response_rows_carry_origin_namespace` — every result row has its concrete 3-seg `namespace`, never the wildcard pattern.

**Scope check (strict on expanded list):**

21. `test_retrieve_wildcard_403_when_token_lacks_read_on_one_expansion_target` — token with `nyla/voice/*:r` only is denied for `nyla/*/episodic` when `nyla/openclaw/episodic` is in the expansion.
22. `test_retrieve_wildcard_200_when_token_has_wildcard_read_scope` — token with `nyla/*/*:r` retrieves `nyla/*/episodic` successfully.
23. `test_retrieve_wildcard_first_403_aborts_no_partial_results` — confirms ADR 0028 strictness still holds under wildcards.

**Write-side reject (locks ADR rule):**

24. `test_episodic_send_with_wildcard_namespace_400s` — `POST /v1/episodic/send` with `nyla/*/episodic` body 400s on the existing namespace regex.
25. `test_thoughts_send_with_wildcard_namespace_400s` — same for `POST /v1/thoughts/send`.

**SDK ergonomics:**

26. `test_sdk_async_retrieve_passes_planes_through` — `AsyncMusubiClient.retrieve(namespace=..., planes=[...])` includes `planes` in the request body.
27. `test_sdk_sync_retrieve_passes_planes_through` — sync client mirror.

**Hypothesis / property:**

28. `hypothesis: any non-wildcard 3-seg namespace passes through expansion unchanged (idempotent on concrete targets)`.
29. `hypothesis: every result row's namespace satisfies the original pattern when checked segment-wise`.

**Explicitly out-of-scope (do NOT implement here):**

- TTL cache for expansion results — deferred per ADR 0031, separate slice when latency or row count crosses threshold.
- Wildcard support in `POST /v1/retrieve/stream` — future trivial follow-up; same expansion helper.
- Wildcard support in `GET /v1/thoughts/stream` `?namespace=` — different shape (subscription), revisit if a use case appears.
- `tenant`/`presence`/`plane` payload fields — option-C path; deferred per ADR 0031.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-24 18:30 — aoi-claude-opus — claim + carve

- Carved in response to Eric's request for tenant-wide retrieve before tomorrow's agent operations. Confirmed shape choice (A — wildcard segments) and no-cache decision in chat.
- ADR 0031 written and referenced; this slice implements it end-to-end.
- Branching `slice/api-retrieve-wildcards`, opening draft PR.

## PR links

- _(pending)_
