---
title: "Slice: SEC-004 — contradictions omitted-namespace scrolls the whole fleet"
slice_id: slice-sec-004-contradictions-fleet-scroll
section: _slices
type: slice
status: in-progress
owner: aoi
phase: "Security audit 2026-07-12 (Eric, discoverer)"
tags: [section/slices, status/in-progress, type/slice, security, p0, auth, scope]
updated: 2026-07-12
reviewed: false
depends-on: []
blocks: [slice-auth-boundary-red-contract]
---

# SEC-004 (C3) — contradictions omitted-namespace scrolls the whole fleet  ·  P0

**Discoverer: Eric.** Source-confirmed by Yua (router). Red tests: Aoi.

## The vulnerability

`GET /v1/contradictions` (`contradictions.py:15`):

```python
dependencies=[Depends(require_auth())],           # <- ordinary auth, NOT operator
async def list_contradictions(namespace: str | None = Query(None), ...):
    scroll_filter = None
    if namespace is not None:
        scroll_filter = <namespace filter>
    records, _ = qdrant.scroll("musubi_concept", scroll_filter=scroll_filter, limit=200, ...)
```

The docstring claims *"cross-namespace by default (operator scope)"* — but the dependency
is plain `require_auth()`, and `require_auth` reads the namespace from the query
(`auth.py:48`). When `namespace` is omitted, `scroll_filter` is `None`, so **any valid
token scrolls the ENTIRE `musubi_concept` collection — every tenant's contradictions.**
The doc says operator; the code demands only ordinary auth.

And the error path (`contradictions.py`):

```python
except Exception:
    return ContradictionListResponse(items=[])
```

A Qdrant failure becomes an **empty 200**, indistinguishable from "no contradictions" —
the RET-007 class (a backend outage silently reported as clean data).

## Scope

Red tests only, contradictions route only (per Yua). No production code; `src/musubi/**`
FORBIDDEN.

`owns_paths`:
- `tests/api/test_sec004_contradictions_scope.py`
- `docs/Musubi/_slices/slice-sec-004-contradictions-fleet-scroll.md`

`forbidden_paths`:
- `src/musubi/**` (auth boundary; ADR-gated fix)

## Test Contract (Yua's six)

`xfail(strict=True)` for the holes; plain asserts for the controls. Synthetic content only.

- [ ] no token → **401**
- [ ] ordinary token + **omitted** namespace → **403** (currently: fleet scroll of all tenants)
- [ ] operator token + omitted namespace → **succeeds**, returns synthetic cross-namespace rows
- [ ] ordinary token + **own** namespace → succeeds, returns **only own** rows
- [ ] ordinary token + **foreign** namespace → **403**
- [ ] backend Qdrant failure → **must NOT become an empty 200** (RET-007 class)

## Inventory

Other nullable-namespace routes guarded by generic `require_auth` (not `require_operator`):
per `tests/api/sec003_route_inventory.py`, **`list_contradictions` is the only one.** The
lifecycle nullable routes are `require_operator` (own authorization; safe — reviewed and
closed by Yua). The scanner now classifies operator-scoped nullable routes as safe.

## Core invariant (Yua, carried)

Omitting the namespace must require **operator** authorization or an explicit authorized
fanout contract — never a silent all-tenant scroll under ordinary auth. And a backend
failure must surface as an error, not as empty data.

## Status
Red tests written and failing (documenting the holes). No fix. Awaiting security-lane ADR.

## Lane disposition (2026-07-12)
Canonical lane is now [[_slices/slice-auth-boundary-red-contract]] (branch `slice/adr-auth-boundary`),
which consolidates and RUNS this slice's reds — this slice is a live dependency of it, not
superseded. The standalone branch `slice/sec-004-contradictions-fleet-scroll` (tip `4031ec0`) is
a direct ANCESTOR of the consolidated branch: **0 unique commits, nothing to cherry-pick.**
Retire-pending; do not delete yet (per Yua process-hygiene REQ 21:52).
