---
title: Namespaces
section: 03-system-design
tags: [architecture, isolation, namespaces, section/system-design, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-24
up: "[[03-system-design/index]]"
reviewed: false
implements: ["src/musubi/api/", "tests/api/"]
---
# Namespaces

How Musubi partitions its data so that agents, channels, and system services coexist without leaking memory across boundaries.

## The namespace triple

Every piece of memory lives in a namespace: `{tenant}/{presence}/{plane}`.

- **tenant** — the agent persona that owns the memory (the continuous "who" across channels). Examples: `nyla`, `aoi`, `hana`, `mizuki`. Also reserved: `system` (lifecycle worker, scheduler).
- **presence** — the channel / client the agent is speaking through. Examples: `voice` (LiveKit), `discord`, `openclaw` (browser plugin), `mcp` (MCP adapter).
- **plane** — one of `episodic`, `curated`, `artifact`, `concept`, `thought`.

Stored as a flat string on every object: `namespace: "nyla/voice/episodic"`.

> Historical note: pre-v1.0, an earlier convention used the human operator as the tenant (`eric/nyla/episodic`). That was flipped to agent-as-tenant in [[13-decisions/0030-agent-as-tenant|ADR 0030]] before v1.0. Example namespaces elsewhere in the vault may still show the old shape; treat this page as the source of truth until the sweep lands.

## How namespaces affect storage

### Qdrant collection strategy

We use **one collection per plane**, with a `namespace` payload field indexed as KEYWORD. Scopes are enforced at *query time* via filter, not at *collection level*.

Collections:
- `musubi_episodic`
- `musubi_curated`
- `musubi_artifact_chunks`
- `musubi_concept`
- `musubi_thought`

Why one-collection-per-plane instead of one-per-agent?
- Agent creation is a config edit (mint a token, add to the fleet), not a Qdrant operation.
- Qdrant payload filtering on an indexed KEYWORD field is O(log n); at our scale (≤ 10M points per plane) this is negligible.
- Snapshot/restore of a shared collection captures all agents atomically.

**If we outgrow this** (say, > 50M points in a plane): we split the largest collection by agent via a zero-downtime migration using Qdrant aliases. See [[11-migration/scaling]].

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
- Exact: `"nyla/voice/episodic"` — single-namespace query.
- 2-segment: `"nyla/voice"` — cross-plane fan (all planes Nyla has written on the voice channel). See [[13-decisions/0028-retrieve-2seg-namespace-crossplane|ADR 0028]].
- Plane-wide via scope glob: tokens with `*/episodic:r` can read every agent's episodic rows; used by household-survey tools (see [[07-interfaces/canonical-api]]).

Qdrant doesn't support prefix match on KEYWORD directly, so plane-wide queries use `should` with enumerated namespaces at the router layer. Enumeration is cheap because the agent registry is ~12 entries.

### The vault

Curated knowledge is partitioned under the vault filesystem, one top-level directory per agent tenant:

```
vault/
├── curated/
│   ├── nyla/                # agent tenant directory
│   │   ├── projects/        # topic directories
│   │   │   ├── musubi.md
│   │   │   └── openclaw.md
│   │   └── personal/
│   │       └── preferences.md
│   ├── aoi/
│   │   └── technical/
│   │       └── gpu-ops.md
│   └── _shared/             # cross-agent shared knowledge (household-read scope required)
│       └── calendar.md
├── artifacts/               # artifact files (namespace-tagged in frontmatter, not path-partitioned)
├── _archive/                # soft-deleted files
└── _inbox/                  # ticket / questions / locks folders
```

The frontmatter in each file declares its namespace explicitly (`tenant: nyla, presence: curated, plane: curated`). The filesystem structure is a convenience for humans (easy nav in Obsidian) — the namespace of record is in the frontmatter, so moving a file across agent folders requires a frontmatter edit too.

## Authorization maps to namespace

A bearer token carries claims:

```json
{
  "sub": "openclaw-livekit-nyla",
  "presence": "nyla/voice",
  "scope": "nyla/*:rw */episodic:r */curated:r",
  "aud": "musubi",
  "iss": "..."
}
```

The auth middleware resolves scope globs against the request's namespace claim. A token scoped `nyla/*:rw` can write and read anywhere under the `nyla/` tenant; a token with additional `*/episodic:r` can survey every agent's episodic plane.

See [[10-security/auth]] for the full token model.

## Special namespaces

- **`system/lifecycle-worker/*`** — the lifecycle worker writes audit events and system-authored synthesized concepts here. Not readable to non-system tokens.
- **`system/scheduler/*`** — scheduled-task-triggered thoughts and reflections.
- **`<agent>/_shared/*`** — shared across all of an agent's channels (e.g., Nyla's canonical preferences, readable whether she's on voice or discord).
- **`_shared/<plane>`** is not a valid namespace — use `<agent>/_shared/<plane>` for per-agent cross-channel, or grant `_shared/*:r` scope for a dedicated shared-knowledge tenant.

## Namespace rules

1. **No defaulting.** Every API call must resolve a namespace. Missing namespace is a 400, not a "use current agent's."
2. **No wildcards in write paths.** Reads can query wildcards (see [§Wildcard reads](#wildcard-reads)); writes must be fully qualified — the canonical regex (`^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*/<plane>$`) rejects `*`.
3. **Cross-namespace relationships are allowed but logged.** A curated file in `nyla/` may cite an artifact in `aoi/`. The audit log records the cross-namespace link.
4. **Namespace strings are case-sensitive, ASCII, kebab-case.**
5. **Namespace cannot be changed after write.** Moving a memory across namespaces is a delete + insert with new lineage.

## Wildcard reads

Per [[13-decisions/0031-retrieve-wildcard-namespace|ADR 0031]], the
`POST /v1/retrieve` namespace accepts `*` as a single-segment wildcard.
Wildcards are read-only — writes still go to a fully-qualified
`<tenant>/<presence>/<plane>` slot, preserving channel provenance on
every row.

| Pattern              | Meaning                                                          |
|----------------------|------------------------------------------------------------------|
| `nyla/voice/episodic`| Single channel, single plane                                     |
| `nyla/voice`         | Single channel, fans across `planes` (ADR 0028)                  |
| `nyla/*/episodic`    | All of Nyla's episodic across her channels — the platform's foundational read pattern: *one agent, many surfaces, one memory* |
| `nyla/*/*` + planes  | Nyla cross-channel × cross-plane                                 |
| `*/voice/episodic`   | Every agent's voice episodic                                     |

`**` is not introduced — segment-count discipline is preserved. A
wildcard segment matches any single non-empty segment in the same
position; literal segments must be exactly equal. The server expands
patterns against the live Qdrant payload, then runs the existing
strict-scope fanout (per ADR 0028) over the resolved targets.

## Multi-instance note

When a second human operator runs their own Musubi instance, we disambiguate by agent-name prefix or suffix (e.g. `nyla-eric` vs `nyla-lisa`) — or keep instances fully separate at the deploy layer. Until a second instance lands, agent names are flat and unique.

## Test Contract

Every plane's module-level tests must include:

- `test_isolation_read_enforcement` — a token for agent `nyla` cannot read `aoi` data at any plane.
- `test_isolation_write_enforcement` — a token for agent `nyla` cannot write with `namespace: aoi/...` in payload.
- `test_household_read_ok` — a token with `nyla/*:rw` plus `*/episodic:r` can read both own and household episodic.
- `test_prefix_query_correctness` — 2-seg queries match all enumerated plane children and nothing else.
- `test_system_namespace_not_readable_from_agent_token` — an agent token cannot read `system/*`.

See [[10-security/auth]] for the full auth test contract.

## Why this is load-bearing

Namespaces are how we keep the small-team model sane. Get this wrong and we either:
- Leak one agent's memory into another's retrieval (privacy / correctness).
- Silo every channel (Nyla-on-voice can't build on Nyla-on-discord's context).
- Can't evolve to multi-instance because the tenancy concept isn't first-class.

Get it right and we get all three: isolation, selective sharing, and future-proof.
