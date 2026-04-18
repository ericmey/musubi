# `proto/` — Musubi gRPC surface

Hand-maintained protobuf definitions for the canonical API. Per
[ADR-0013](../13-decisions/0013-api-spec-authoring.md), pydantic models in
`musubi/types/` are the source of truth; this folder is a compatibility mirror
enforced by `tests/contract/test_proto_parity.py`.

## Layout

```
proto/
├── buf.yaml        # lint + breaking-change config
├── buf.gen.yaml    # codegen recipe (Python + TypeScript)
└── musubi/v1/
    └── musubi.proto
```

## Commands

```bash
buf lint           # style + correctness
buf breaking --against '.git#branch=main'   # breaking-change detection
buf generate       # emit Python/TS stubs
```

## Editing rules

- **Additive fields:** new field number, never reuse. Bump no version.
- **Removed fields:** mark `reserved 7;` (or whatever number) plus `reserved "old_name";`.
- **New message type:** requires an ADR.
- **Breaking semantic change:** new package `musubi.v2`, don't touch v1.
- **Parity:** every new pydantic public model gets a matching message in the same PR.

## Relation to OpenAPI

OpenAPI is generated at runtime by FastAPI from the pydantic layer. It is
snapshotted at `07-interfaces/openapi/musubi.v1.yaml` on each major bump. Proto
covers the gRPC surface (including streaming); OpenAPI covers HTTP. Both mirror
the same pydantic types.
