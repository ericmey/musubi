---
title: "Cross-slice: wire production plane factories into create_app()"
section: _inbox/cross-slice
type: cross-slice
source_slice: slice-ops-integration-harness
target_slice: slice-api-app-bootstrap
status: resolved
resolved_by: slice-api-app-bootstrap (PR #126)
opened_by: vscode-cc-sonnet47
opened_at: 2026-04-19
resolved_at: 2026-04-20
tags: [section/inbox-cross-slice, type/cross-slice, status/resolved]
updated: 2026-04-20
---

> **Resolved.** `slice-api-app-bootstrap` (PR #126) shipped
> `src/musubi/api/bootstrap.py` with `bootstrap_production_app(app, settings)`.
> `create_app()` now calls it on the way up gated on
> `settings.musubi_skip_bootstrap` + presence of pre-installed
> dependency overrides. Integration harness bullets 5/6/7/9/12 unskipped
> in the same PR. This ticket is closed; left in the vault as the audit
> trail.


# Wire production plane factories into `create_app()`

## Source slice

`slice-ops-integration-harness` (PR #114).

## Problem

`musubi.api.dependencies` ships every plane factory as
`raise NotImplementedError(...)` per the ADR-punted-deps-fail-loud
pattern:

```python
def get_episodic_plane() -> EpisodicPlane:
    raise NotImplementedError(
        "EpisodicPlane is not configured. Override "
        "app.dependency_overrides[get_episodic_plane] in tests, or wire "
        "production deps via the deploy-side bootstrap (slice-ops-compose). "
        "Failing closed per the ADR-punted-deps-fail-loud rule."
    )
```

The unit suite (`tests/api/conftest.py`) overrides these in tests via
`app.dependency_overrides` against in-memory Qdrant + `FakeEmbedder`,
which is correct for unit isolation.

But `create_app()` itself — the production entry point — has NO
bootstrap that wires real `EpisodicPlane(client=qdrant_client,
embedder=tei_embedder)` etc. on the way up. Until something does,
the production app comes up but every plane endpoint returns 500
on first hit.

This was hidden until tonight because nothing was actually invoking
the production app outside of unit tests. The integration harness
(slice-ops-integration-harness, PR #114) is the first thing to spin
up `create_app()` against live dependencies, and the very first
smoke bullet (`test_capture_then_retrieve_roundtrip`) surfaced
`NotImplementedError: EpisodicPlane is not configured`.

## Affected bullets in the integration harness

In `tests/integration/test_smoke.py` (PR #114), these bullets are
currently skipped against this ticket:

- 5  `test_capture_then_retrieve_roundtrip`
- 6  `test_capture_dedup_against_existing`
- 7  `test_thought_send_check_read_history`
- 9  `test_curated_create_then_retrieve`
- 12 `test_artifact_upload_multipart_then_retrieve_blob`

Bullets 8 (SSE), 10/11 (concept synthesis), 13/14 (perf budgets) are
already skipped against their own follow-ups (#120 + concept-synthesis
worker-trigger followup + the strict-mode env-var gate).

## Requested change

Add a production bootstrap to `create_app()` (or a sibling
`bootstrap_production_app()`) that:

1. Reads `Settings` from the env (already done).
2. Constructs a `QdrantClient` against `settings.qdrant_host:port`.
3. Constructs a real `Embedder` against the TEI services
   (`settings.tei_dense_url` etc.).
4. Wires every plane factory (`get_episodic_plane`,
   `get_curated_plane`, `get_concept_plane`, `get_artifact_plane`)
   to return an instance built with the above clients.
5. Wires lifecycle / ingestion services similarly.

The unit-test override pattern (`app.dependency_overrides[...] = ...`)
keeps working — this just supplies the production default that's
missing today.

## Acceptance

- `create_app()` (or its production sibling) wires every plane
  factory to a real instance.
- Re-running the integration harness's bullets 5-9 + 12 against the
  unmodified compose stack passes.
- Skips removed from `tests/integration/test_smoke.py` for those
  five bullets.
- Unit-suite path unchanged (overrides still win).

## Why this should land before any other slice unskips its own
bullet

Every consumer-slice unskip path through the harness routes
through `create_app()` boot. Without this gap closed, even
follow-up PRs that wire SSE / synthesis / perf can't get green.

## Owner suggestion

slice-api-v0-write produced the canonical write-side endpoints; the
production bootstrap is most naturally an extension of that slice's
ownership. Could also live in slice-api-v0-read or a new
`slice-api-app-bootstrap` carve-out.
