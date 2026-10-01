---
title: Contract Tests
section: 07-interfaces
tags: [adapters, contract, interfaces, section/interfaces, status/complete, testing, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[07-interfaces/index]]"
reviewed: false
implements: ["src/musubi/api/", "tests/api/"]
---
# Contract Tests

A shared, black-box contract suite that every integration runs against a live Musubi (a separate `musubi-contract-tests` package) was planned and **has not been built**. This page records what covers the API contract today and what remains future work.

## What exists today

All in this repository:

| Area | Tests |
|---|---|
| HTTP API: routes, auth and scope, idempotency, rate limits, retrieval wire shapes, streaming, context packs | `tests/api/` |
| OpenAPI drift: committed `openapi.yaml` against runtime routes | `tests/api/test_api_v0_read.py::test_runtime_openapi_matches_committed_paths`, `test_committed_openapi_yaml_includes_read_paths`; `tests/api/test_api_v0_write.py::test_committed_openapi_yaml_includes_write_paths` |
| In-repo Python SDK (`musubi.sdk`) | `tests/sdk/` |
| In-repo MCP adapter and the LiveKit compatibility shim | `tests/adapters/` |
| Live-stack integration (real Qdrant, TEI, LLM) | `tests/integration/` (`make test-integration`, nightly in CI) |

Sibling integration repositories (`musubi-sdk`, `musubi-livekit`, `musubi-openclaw`, `musubi-claude` and the rest) run their own tests against their own fixtures. No shared suite gates them.

Some tests in `tests/api/test_api_v0_read.py` are explicitly skipped with the reason "deferred to musubi-contract-tests repo" (suite meta-tests, latency budgets, cross-endpoint error shape, multi-run isolation, version pinning). They remain skipped until the shared suite exists.

## Smoke

There is no `smoke` suite in a contract package. Deploy verification uses:

- `deploy/smoke/verify.sh`, which runs `check_api.sh`, `check_capture.sh`, `check_thoughts.sh` and `check_observability.sh` against a running deployment;
- `tests/integration/test_smoke.py`, the live-stack smoke tests run by `make test-integration`.

## Future work: a shared suite

If built, the shared suite should:

- be a black-box pytest package that installs independently of Core and points at a running instance through a URL and token;
- cover capture (happy path, dedup, idempotency, size limit), retrieve (modes, namespace shapes, scope failures), thoughts (send, check, read, stream replay), artifacts (upload, indexing state), errors (envelope and codes) and rate limits;
- include an agent-tools subset (see [[07-interfaces/agent-tools]]) that each integration runs through its own tool layer;
- be versioned against the API major version.

Until then, a change to the API surface is checked by `tests/api/` plus the OpenAPI drift tests, and integrations find breakage through their own CI.

## Test Contract

Not applicable until the shared suite exists. Current API contract coverage is listed under "What exists today".
