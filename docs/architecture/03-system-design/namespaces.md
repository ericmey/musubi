---
title: Namespaces
section: 03-system-design
tags: [architecture, isolation, namespaces, section/system-design, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[03-system-design/index]]"
reviewed: false
---
# Namespaces

How Musubi partitions its data so that multiple tenants and presences coexist without leaking memory across boundaries.

## The namespace triple

Every piece of memory lives in a namespace: `{tenant}/{presence}/{plane}`.

- **tenant** — a human identity or shared household bucket. Examples: `eric`, `household`.
- **presence** — an AI agent identity. Examples: `claude-code`, `livekit-voice`, `openclaw`.
- **plane** — one of `episodic`, `curated`, `artifact`, `concept`, `thought`.

Stored as a flat string on every object: `namespace: "eric/claude-code/episodic"`.

## How namespaces affect storage

### Qdrant collection strategy

We use **one collection per plane**, with a `namespace` payload field indexed as KEYWORD. Scopes are enforced at *query time* via filter, not at *collection level*.

Collections:
- `musubi_episodic`
- `musubi_curated`
- `musubi_artifact_chunks`
- `musubi_concept`
- `musubi_thought`

Why one-collection-per-plane instead of one-per-tenant?
- With ~5 tenants × 5 planes = 25 collections, storage overhead is manageable, but every new tenant requires a provisioning step. Keeping data in a shared collection means tenant creation is a config edit, not a Qdrant operation.
- Qdrant payload filtering on an indexed KEYWORD field is O(log n); at our scale (≤ 10M points per plane) this is negligible.
- Snapshot/restore of a shared collection captures all tenants atomically.

**If we outgrow this** (say, > 50M points in a plane): we split the largest collection by tenant via a zero-downtime migration using Qdrant aliases. See [[11-migration/scaling]].

### Filter on every query

Every retrieval function adds an implicit filter:

```python
Filter(
    must=[
        FieldCondition(key="namespace", match=MatchValue(value=ns_expr))
    ]
)
```

where `ns_expr` is one of:
- Exact: `"eric/claude-code/episodic"` — single-namespace query.
- Prefix: `"eric/claude-code/"` — all planes for this presence (cross-plane blended).
- Tenant-wide: `"eric/"` — all presences, all planes, for this tenant.

Qdrant doesn't support prefix match on KEYWORD directly, so tenant-wide or presence-wide queries use `should` with all enumerated namespaces. Enumeration is cheap because the presence registry is ~10 entries.

### The vault

Curated knowledge is partitioned under the vault filesystem:

```
vault/
├── curated/
│   ├── eric/              # tenant directory
│   │   ├── projects/      # topic directories
│   │   │   ├── musubi.md
│   │   │   └── openclaw.md
│   │   └── personal/
│   │       └── preferences.md
│   └── household/
│       └── shared-calendar.md
├── artifacts/             # artifact files (namespace-tagged in frontmatter, not path-partitioned)
├── _archive/              # soft-deleted files
└── _inbox/                # ticket / questions / locks folders
```

The frontmatter in each file declares its namespace explicitly (`tenant: eric`, `plane: curated`). The filesystem structure is a convenience for humans (easy nav in Obsidian) — the namespace of record is in the frontmatter, so moving a file across tenant folders requires a frontmatter edit too.

## Authorization maps to namespace

A bearer token carries claims:

```json
{
  "sub": "claude-code@eric",
  "tenant": "eric",
  "presence": "claude-code",
  "scopes": ["memory:read", "memory:write", "curated:read"]
}
```

The auth middleware resolves this into an allowed namespace prefix and injects it into every query. A request with token for `eric/claude-code` cannot retrieve from `household/*` unless the token explicitly carries `tenant: household` as an additional allowed tenant (home-shared agents may).

See [[10-security/auth]] for the full token model.

## Special namespaces

- **`system/lifecycle-worker/*`** — the lifecycle worker writes audit events and system-authored synthesized concepts here. Not readable to non-system tokens.
- **`system/scheduler/*`** — scheduled-task-triggered thoughts and reflections.
- **`{tenant}/_shared/*`** — shared across all presences of a tenant (e.g., tenant-level curated knowledge that any presence can read).
- **`household/*`** — a tenant for multi-person shared knowledge (family calendar, home operations). Explicitly opted into by tokens.

## Namespace rules

1. **No defaulting.** Every API call must resolve a namespace. Missing namespace is a 400, not a "use current user's."
2. **No wildcards in write paths.** Reads can query prefixes; writes must be fully qualified.
3. **Cross-namespace relationships are allowed but logged.** A curated file in `eric/` may cite an artifact in `household/`. The audit log records the cross-namespace link.
4. **Namespace strings are case-sensitive, ASCII, kebab-case.**
5. **Namespace cannot be changed after write.** Moving a memory across namespaces is a delete + insert with new lineage.

## Test contract

Every plane's module-level tests must include:

- `test_isolation_read_enforcement` — a token for `tenant-a` cannot read `tenant-b` data at any plane.
- `test_isolation_write_enforcement` — a token for `tenant-a` cannot write with `tenant: tenant-b` in payload.
- `test_shared_read_ok` — a token with `tenant: eric` and `allowed_tenants: [household]` can read both.
- `test_prefix_query_correctness` — prefix queries match all enumerated children and nothing else.
- `test_system_namespace_not_readable_from_user_token` — a user token cannot read `system/*`.

See [[10-security/auth]] for the full auth test contract.

## Why this is load-bearing

Namespaces are how we keep the small-team model sane. Get this wrong and we either:
- Leak household memory into individual-presence retrieval (privacy / correctness).
- Silo every presence (agents can't build on each other's context).
- Can't ever evolve to multi-tenant because the concept isn't first-class.

Get it right and we get all three: isolation, selective sharing, and future-proof.
