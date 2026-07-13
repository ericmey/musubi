---
title: "Slice: SEC-003 — namespace outside the query string bypasses scope auth"
slice_id: slice-sec-003-namespace-outside-query
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

# SEC-003 (C2) — namespace outside the query string bypasses scope auth  ·  P0

**Discoverer: Eric.** Source-confirmed by Yua (router). Red tests + full inventory: Aoi.

## The mechanic

`require_auth` reads the namespace it authorizes **only from the query string**
(`src/musubi/api/auth.py:48`):

```python
ns = request.query_params.get(namespace_qs_param) if not operator else None
```

So on any route whose namespace arrives via **Form, Path, or Body**, `ns` is `None`, the
namespace scope check is skipped/defanged, and a token authorizes nothing about the
target it actually operates on. A valid token for tenant B can act on tenant A's
namespace.

## Full route inventory (every require_auth route; how each sources its namespace)

Auth CAN see the namespace (query param) — NOT affected:
- `episodic.py`, `curated.py`, `concept.py` (`concepts.py`), `artifacts.py` reads,
  `contradictions.py` — all `namespace: str = Query(...)`.

**Auth CANNOT see the namespace — AFFECTED:**

| route | file:line | namespace source | issue |
| --- | --- | --- | --- |
| `POST /v1/artifacts` (`upload_artifact`) | `writes_artifact.py:37` | `Form(...)` | require_auth(access="w") sees empty query → `ns=None`; write-scope defanged on multipart upload |
| `GET /v1/namespaces/{namespace_path}/stats` (`namespace_stats`) | `namespaces.py:58` | `Path(...)` | passes `namespace_qs_param="namespace_path"`, but the value is a PATH param; query `?namespace_path=` is empty → auth checks nothing |

**Mechanical evidence:** `tests/api/sec003_route_inventory.py` scans every router and
classifies each `require_auth` route by namespace source — a new Form/Path/Body route
appears there automatically, so a future addition cannot hide behind prose. Current run:
2 AFFECTED (upload_artifact FORM, namespace_stats PATH), and it additionally flags 2
NULLABLE-query routes for SEC-004 scope (`contradictions.list_contradictions`,
`lifecycle.list_events`).

Adjacent (checked, reported separately): `contradictions.py` uses
`namespace: str | None = Query(None)` — nullable-namespace fanout is **SEC-004 (C3)**, not
this slice.

## Scope

Red tests + inventory + design. No production code; `src/musubi/**` FORBIDDEN.

`owns_paths`:
- `tests/api/test_sec003_namespace_scope.py`
- `docs/Musubi/_slices/slice-sec-003-namespace-outside-query.md`

`forbidden_paths`:
- `src/musubi/**` (auth is a frozen boundary; fix is ADR-gated / `slice-api-v*`)

## Test Contract (Yua)

`xfail(strict=True)` — asserts the secure behaviour, fails today, flips green when fixed.
Synthetic content only.

- [ ] `upload_artifact`: a VALID token for tenant B uploading to tenant A's namespace **must be 403**
- [ ] `upload_artifact`: same-tenant authorized upload **must still succeed** (feature preserved)
- [ ] `namespace_stats`: cross-tenant token reading another namespace's stats **must be 403**
- [ ] `namespace_stats`: own-namespace stats **must still succeed**
- [ ] no-token on either route **must be 401**

## Core invariant (Yua, carried from SEC-002)

Every operation must pass the **same authorization the equivalent query-namespace route
would require**, regardless of where the namespace physically arrives (query / form / path
/ body). Reading the namespace only from the query string is the defect; the fix resolves
the effective namespace from the route's real source and authorizes THAT.

## Status
Red tests written and failing (documenting the hole). No fix. Awaiting security-lane ADR.

## Lane disposition (2026-07-12)
Canonical lane is now [[_slices/slice-auth-boundary-red-contract]] (branch `slice/adr-auth-boundary`),
which consolidates and RUNS this slice's reds — this slice is a live dependency of it, not
superseded. The standalone branch `slice/sec-003-namespace-outside-query` (tip `88a8ba9`) is a
direct ANCESTOR of the consolidated branch: **0 unique commits, nothing to cherry-pick.**
Retire-pending; do not delete yet (per Yua process-hygiene REQ 21:52).
