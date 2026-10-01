# `proto/`: protobuf reference mirror

**No gRPC server ships.** Musubi Core serves HTTP only. The `MUSUBI_GRPC`
setting exists in `src/musubi/settings.py` but no server code reads it, and no code
generated from these files is used anywhere in the repo.

This folder is a hand-maintained **reference mirror** of the canonical API
types, kept from the original design in
[ADR-0013](../13-decisions/0013-api-spec-authoring.md). Pydantic models in
`src/musubi/types/` and the API routers are the source of truth; the HTTP
contract is `openapi.yaml` at the repo root.

What is **not** in place, despite what older text (including comments in
`musubi.proto` and ADR-0013) says:

- There is no proto parity test. `tests/contract/test_proto_parity.py` does
  not exist, so the mirror can drift from the pydantic models unnoticed.
- No CI job runs `buf lint`, `buf breaking` or `buf generate`.
- The tests that would compare a gRPC transport with REST
  (`test_protobuf_via_grpc_matches_rest_semantics*` in `tests/api/`) are
  skipped.

Treat the `.proto` file as a sketch of a possible future surface, not as a
description of the running system.

## Layout

```
proto/
├── buf.yaml        # lint + breaking-change config
├── buf.gen.yaml    # codegen recipe (Python + TypeScript)
└── musubi/v1/
    └── musubi.proto
```

## If you edit it

`buf` is not installed or run by the repo. If you use it locally:

```bash
buf lint
buf breaking --against '.git#branch=main'
```

Keep the usual protobuf rules: never reuse a field number, mark removed
fields `reserved`, and put breaking changes in a new `musubi.v2` package.
Changes to `proto/` still follow the API rule in AGENTS.md (an ADR if
additive, a version bump if breaking).
