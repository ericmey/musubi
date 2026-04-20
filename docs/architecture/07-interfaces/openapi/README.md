---
title: "OpenAPI snapshots"
section: 07-interfaces/openapi
type: index
status: complete
tags: [section/interfaces, status/complete, type/index, api]
updated: 2026-04-17
up: "[[07-interfaces/index]]"
reviewed: true
---

# OpenAPI snapshots

Per [[13-decisions/0013-api-spec-authoring]], pydantic models in `musubi/types/` are the source of truth for the canonical API. FastAPI generates OpenAPI 3.1 from them at runtime and serves it at `GET /v1/openapi.json`.

This folder holds **frozen snapshots** taken on each API version bump. Consumers that need a stable file (code generators, public docs, vendor contract reviews) pin to a snapshot here.

## Files

- `musubi.v1.yaml` — current snapshot of `/v1/*`. Regenerated only on version-major events.
- `musubi.v1.schemas.json` — companion JSON Schema 2020-12 dump. Produced from `model.model_json_schema()` across all public pydantic models. Consumed by the TypeScript SDK.

## How snapshots are refreshed

1. Boot musubi-core locally with the target build.
2. Run `python -m musubi.scripts.dump_openapi > 07-interfaces/openapi/musubi.v1.yaml`.
3. Run `python -m musubi.scripts.dump_json_schema > 07-interfaces/openapi/musubi.v1.schemas.json`.
4. Commit with message `api(v1): snapshot openapi @ <short-sha>`.
5. If this introduces a breaking change, the major bumps: create `musubi.v2.yaml`, never overwrite `musubi.v1.yaml`.

## How agents use this

- Implementing the API (`slice-api-v0`): don't edit the yaml by hand. Edit pydantic + routes, rebuild, re-dump.
- Implementing an adapter: read the yaml or the live endpoint; treat it as frozen.
- Building a TypeScript SDK: generate from `musubi.v1.schemas.json` via `json-schema-to-typescript`.

## Related

- [[07-interfaces/canonical-api]] — the human-readable spec.
- [[07-interfaces/contract-tests]] — the proto-parity test.
- [[13-decisions/0013-api-spec-authoring]] — the authoring-model ADR.
- Proto mirror: `proto/musubi/v1/*.proto`.
