---
title: "ADR 0013: API spec authoring model"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-17
deciders: [Eric]
tags: [section/decisions, status/accepted, type/adr]
updated: 2026-04-17
up: "[[13-decisions/index]]"
reviewed: true
---

# ADR 0013: API spec authoring model

**Status:** accepted
**Date:** 2026-04-17
**Deciders:** Eric

## Context

Musubi exposes HTTP (REST-shaped) and gRPC surfaces. Three artifacts describe that API:

1. **Pydantic models** in `musubi/types/` — used directly by the FastAPI routes and the planes.
2. **OpenAPI 3.1 document** — consumed by Swagger UI, by the Python SDK (generated client), by the TypeScript SDK (generated), and by the contract-tests suite.
3. **Protobuf + gRPC** — consumed by gRPC clients (LiveKit adapter, potentially future adapters).

Authoring all three by hand guarantees drift. We need a single source of truth and a rule for how the others are produced.

Options considered:

- **Option A — Proto-first.** Write `.proto` files as the authoritative schema; generate pydantic via `betterproto` or `protobuf-pydantic`; generate OpenAPI via `grpc-gateway` or `buf`. Strictest typing, best cross-language story, but heavy tooling and painful local iteration.
- **Option B — OpenAPI-first.** Hand-author `openapi.yaml`; generate pydantic via `datamodel-code-generator`; generate proto via `openapi2proto` (quality varies). Good for documentation-driven teams, but generators for pydantic are awkward with Musubi's strict typing.
- **Option C — Pydantic-first.** Pydantic models are the source of truth. FastAPI generates OpenAPI 3.1 at runtime from route signatures. Proto is hand-maintained and reviewed as a mirror of pydantic, enforced by a compatibility test.
- **Option D — Three independent artifacts.** Accept the drift tax, reconcile via contract tests.

## Decision

**Option C — Pydantic-first with derived OpenAPI and mirror-proto.**

- Pydantic v2 models in `musubi/types/` are **the source of truth**. Every public payload is a pydantic model.
- FastAPI produces the **OpenAPI 3.1 document at runtime** from route decorators + pydantic models. The derived document is served at `GET /v1/openapi.json` and committed as a **frozen snapshot** at `07-interfaces/openapi/musubi.v1.yaml` on each API version bump.
- Protobuf / gRPC lives in `proto/musubi/v1/*.proto`. It is **hand-maintained** and serves as a mirror of the pydantic types. A compatibility test (`tests/contract/test_proto_parity.py`) asserts field-by-field parity; drift fails CI.
- `buf` is used for proto linting, breaking-change detection, and codegen. Proto changes are reviewed via ADR if they introduce new message types (not required for additive fields within an existing message).
- JSON Schema (2020-12) is emitted alongside the snapshot from pydantic via `model_json_schema()` for TypeScript SDK consumption.

## Consequences

### Positive

- **One authoritative surface.** Agents reading `musubi/types/episodic.py` know exactly what the API promises.
- **FastAPI doing what it's good at.** We don't fight the framework.
- **Contract tests enforce proto parity** — drift is detected at commit time, not at integration time.
- **No generator in the hot path.** OpenAPI ships at runtime; the committed snapshot is a public artifact, not a build input.

### Negative

- **Proto is hand-maintained**, which is where drift enters. The parity test mitigates but doesn't eliminate the cost.
- **gRPC-specific features** (streaming, server-reflection metadata) have no pydantic equivalent. Those must live on the proto side only and are called out in [[07-interfaces/canonical-api]] under a "gRPC-only" subsection.
- **SDK code generation** is still needed for the TypeScript SDK; our Python SDK imports pydantic types directly rather than generating a client.

### Neutral

- Versioning: the API major is signalled both in the URL (`/v1/`) and in the proto package (`musubi.v1`). Bumping major means a new OpenAPI snapshot and a new proto package side-by-side for a deprecation window.

## Alternatives considered

### A — Proto-first

Rejected because:
- `betterproto` generates classes that look nothing like idiomatic pydantic; every field access gets heavier.
- Iteration loop (edit proto → regen → edit Python) slows local dev.
- gRPC-specific features leak into non-gRPC code.

### B — OpenAPI-first

Rejected because:
- Pydantic code generated from OpenAPI is awkward (aliases, Union types, Enum handling).
- Loses typed Python inheritance — our pydantic layer uses shared base classes.
- FastAPI already produces OpenAPI for us; inverting that is work for no benefit.

### D — Independent artifacts + contract tests only

Rejected because:
- Three sources of truth is three places to break.
- Contract tests can only catch drift post-hoc; we want drift prevention at authoring time.

## References

- Pydantic v2 JSON Schema emission: `BaseModel.model_json_schema()`.
- FastAPI OpenAPI emission: `app.openapi()` and `docs_url="/docs"`.
- [[07-interfaces/canonical-api]] — the human-readable spec that both OpenAPI and proto are the machine-readable embodiments of.
- [[07-interfaces/contract-tests]] — the proto-parity test lives here.
- [[_slices/slice-api-v0]] — the slice that owns this.
