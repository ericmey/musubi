---
title: "OpenAPI snapshots"
section: 07-interfaces/openapi
type: index
status: complete
tags: [section/interfaces, status/complete, type/index, api]
updated: 2026-10-01
up: "[[07-interfaces/index]]"
reviewed: true
---

# OpenAPI snapshots

**The normative API contract is the committed `openapi.yaml` at the repository root**, not anything in this folder.

Per [[13-decisions/0013-api-spec-authoring]], the FastAPI routes and their pydantic request and response models in `src/musubi/api/` are the source of truth. FastAPI generates the OpenAPI document at runtime and serves it at `GET /v1/openapi.json` (interactive docs at `/v1/docs`). The committed root `openapi.yaml` is the reviewed snapshot of that document. `tests/api/test_api_v0_read.py::test_runtime_openapi_matches_committed_paths` fails when the runtime paths and the committed paths diverge.

## Files in this folder

- `musubi.v1.yaml`: an early hand-written skeleton from before the API was built. It is **stale**: it does not match the shipped API and is not maintained. ADR 0013 and ADR 0035 still mention it, which is why it has not been deleted. Do not use it for code generation or review.

There is no JSON Schema dump, no snapshot-dump script and no TypeScript SDK generated from this folder.

## Refreshing the root snapshot

1. Make the route and model changes in `src/musubi/api/`.
2. Regenerate the document from the app (for example, boot Core and fetch `/v1/openapi.json`) and update the root `openapi.yaml`.
3. Run `uv run pytest -q tests/api/test_api_v0_read.py tests/api/test_api_v0_write.py`; the drift tests compare committed paths with runtime paths.
4. An additive change needs an ADR; a breaking change needs a new major version (`/v2/`).

## How agents use this

- Implementing the API: edit routes and models, then update the root `openapi.yaml` in the same PR. Never hand-edit it to describe behaviour the code does not have.
- Writing a client: read the root `openapi.yaml` or the live `/v1/openapi.json`.

## Related

- [[07-interfaces/canonical-api]]: the human-readable spec.
- [[13-decisions/0013-api-spec-authoring]]: the authoring-model ADR.
