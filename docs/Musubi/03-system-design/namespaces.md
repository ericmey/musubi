---
title: Namespaces
section: 03-system-design
tags: [architecture, isolation, namespaces, section/system-design, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[03-system-design/index]]"
reviewed: false
implements: ["src/musubi/api/", "tests/api/"]
---
# Namespaces

How Musubi partitions its data so that agents, channels, and system services coexist without leaking memory across boundaries.

## The namespace triple

Every piece of memory lives in a namespace: `{tenant}/{presence}/{plane}`.

- **tenant** — the agent persona that owns the memory (the continuous "who" across channels). Examples: `alex`, `sam`.
- **presence** — the channel / client the agent is speaking through. Examples: `voice` (LiveKit), `discord`, `openclaw` (browser plugin), `claude-code`, `shared` (knowledge shared across the agent's channels).
- **plane** — one of `episodic`, `curated`, `concept`, `artifact`, `thought`, `lifecycle` (`src/musubi/types/common.py:49-53`).

Stored as a flat string on every object: `namespace: "alex/voice/episodic"`.

> Historical note: pre-v1.0, an earlier convention used the human operator as the tenant (`admin/alex/episodic`). That was flipped to agent-as-tenant in [[13-decisions/0030-agent-as-tenant|ADR 0030]] before v1.0.

## How namespaces affect storage

### Qdrant collection strategy

We use **one collection per plane**, with a `namespace` payload field indexed as KEYWORD. Scopes are enforced at *query time* via filter, not at *collection level*.

Collections (`src/musubi/store/names.py`):
- `musubi_episodic`
- `musubi_curated`
- `musubi_concept`
- `musubi_artifact` (artifact metadata) and `musubi_artifact_chunks` (indexed chunks)
- `musubi_thought`
- `musubi_lifecycle_events` (declared; the lifecycle-event mirror is not populated yet)

Why one-collection-per-plane instead of one-per-agent?
- Agent creation is a config edit (mint a token, add to the fleet), not a Qdrant operation.
- Filtering on an indexed KEYWORD field is cheap at small-team scale.
- Snapshot/restore of a shared collection captures all agents atomically.

### Filter on every query

The retrieve route first resolves the request into concrete `(namespace, plane)` targets, checks scope on each one ([[10-security/auth]]), and then queries each target with an exact namespace filter:

```python
Filter(
    must=[
        FieldCondition(key="namespace", match=MatchValue(value="alex/voice/episodic"))
    ]
)
```

The request's `namespace` can be:
- Exact: `"alex/voice/episodic"` — single-namespace query.
- 2-segment: `"alex/voice"` — cross-plane fan (one target per requested plane). See [[13-decisions/0028-retrieve-2seg-namespace-crossplane|ADR 0028]].
- A wildcard pattern such as `"alex/*/episodic"` — expanded against the stored namespaces; see [§Wildcard reads](#wildcard-reads).
- Omitted — recall across the caller's own tenant: Core enumerates the stored namespaces for the token's tenant and keeps the ones the token can read.

Reading another agent's rows needs a scope that covers them, such as `*/*/episodic:r` (every agent's episodic plane). Scope globs match segment by segment, so they must have as many segments as the namespace.

### The vault

Curated knowledge is plain Markdown under the vault. When the lifecycle engine promotes a concept, it writes the file at
`curated/<tenant>/<presence>/<topic>/<slug>.md` (`src/musubi/lifecycle/promotion.py:133-159`):

```
vault/
└── curated/
    ├── alex/                  # tenant
    │   ├── shared/            # presence
    │   │   └── projects/      # topic
    │   │       └── musubi.md
    │   └── voice/
    │       └── personal/
    │           └── preferences.md
    └── sam/
        └── discord/
            └── technical/
                └── gpu-ops.md
```

The frontmatter in each file declares its namespace explicitly as one field, `namespace: alex/shared/curated`. The directory layout is a convenience for humans; the namespace of record is the frontmatter, so moving a file to another agent's folder requires a frontmatter edit too.

## Authorization maps to namespace

A bearer token carries claims:

```json
{
  "sub": "alex/voice",
  "presence": "alex/voice",
  "scope": "alex/*/*:rw */*/episodic:r */*/curated:r",
  "aud": "musubi",
  "iss": "..."
}
```

`sub` must equal `presence` (`src/musubi/auth/tokens.py:225-226`), and every scope that names a concrete tenant must name the presence's tenant. Scope globs match segment by segment and must have the same number of segments as the namespace (`src/musubi/auth/scopes.py:220-232`): `alex/*/*:rw` reads and writes every 3-segment namespace under `alex/`; `*/*/episodic:r` reads every agent's episodic plane. Neither covers a 2-segment namespace such as `alex/voice`; add `alex/*:r` for that.

See [[10-security/auth]] for the full token model.

## Special namespaces

- **There is no `system` tenant.** Background jobs write under ordinary namespaces. The reflection job writes its curated reflections to `lifecycle-worker/ops/curated` (`src/musubi/lifecycle/runner.py:693`), and the worker sends thoughts as the presence `lifecycle-worker`. Nothing reserves these names; read access follows the normal scope rules.
- **`<agent>/shared/<plane>`** — knowledge shared across all of an agent's channels (e.g. Alex's canonical preferences in `alex/shared/curated`, readable whether she's on voice or discord). `shared` is an ordinary presence name, not a special case in code.
- **A segment cannot start with `_` or `-`.** `alex/_shared/curated` fails namespace validation; use `alex/shared/curated`.

## Namespace rules

1. **Writes always name a namespace.** Every capture, patch and delete carries a fully qualified namespace; there is no "use the token's presence" default. The one read-side exception is `POST /v1/retrieve` without a namespace, which recalls across the caller's own tenant, filtered by scope.
2. **No wildcards in write paths.** Reads can query wildcards (see [§Wildcard reads](#wildcard-reads)); writes must be fully qualified — the canonical regex (`^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*/(episodic|curated|concept|artifact|thought|lifecycle)$`) rejects `*`.
3. **Cross-namespace relationships are allowed.** A curated file in `alex/` may cite an artifact in `sam/`.
4. **Namespace strings are lowercase ASCII.** Each segment starts with a letter or digit; `-` and `_` are allowed after that.
5. **Namespace cannot be changed after write.** Moving a memory across namespaces is a delete + insert with new lineage.

## Wildcard reads

Per [[13-decisions/0031-retrieve-wildcard-namespace|ADR 0031]], the
`POST /v1/retrieve` namespace accepts `*` as a single-segment wildcard.
Wildcards are read-only — writes still go to a fully-qualified
`<tenant>/<presence>/<plane>` slot, preserving channel provenance on
every row.

| Pattern              | Meaning                                                          |
|----------------------|------------------------------------------------------------------|
| `alex/voice/episodic`| Single channel, single plane                                     |
| `alex/voice`         | Single channel, fans across `planes` (ADR 0028)                  |
| `alex/*/episodic`    | All of Alex's episodic across her channels — the platform's foundational read pattern: *one agent, many surfaces, one memory* |
| `alex/*/*` + planes  | Alex cross-channel × cross-plane                                 |
| `*/voice/episodic`   | Every agent's voice episodic                                     |

`**` is not accepted as a retrieve namespace — segment-count discipline is preserved. A
wildcard segment matches any single non-empty segment in the same
position; literal segments must be exactly equal. The server expands
patterns against the live Qdrant payload, then runs the strict per-target
scope check (per ADR 0028) over the resolved targets.

## Multi-instance note

When a second human operator runs their own Musubi instance, we disambiguate by agent-name prefix or suffix (e.g. `alex-acme` vs `alex-globex`) — or keep instances fully separate at the deploy layer. Until a second instance lands, agent names are flat and unique.

## Test Contract

Every plane's module-level tests include:

- `test_isolation_read_enforcement` — a query or `get` in one namespace never returns an object stored in another (episodic, curated, concept).
- `test_isolation_write_enforcement` — a mutation that names the wrong namespace fails instead of changing the object (episodic, curated, concept).

Token-level isolation (an `alex` token cannot read or write `sam` data) is covered by the scope tests in `tests/auth/test_auth.py`.

Not yet written (the contract is open):

- `test_cross_agent_read_ok` — a token with `alex/*/*:rw` plus `*/*/episodic:r` can read both its own and every other agent's episodic.
- `test_prefix_query_correctness` — 2-seg queries match all enumerated plane children and nothing else.

See [[10-security/auth]] for the full auth test contract.

## Why this is load-bearing

Namespaces are how we keep the small-team model sane. Get this wrong and we either:
- Leak one agent's memory into another's retrieval (privacy / correctness).
- Silo every channel (Alex-on-voice can't build on Alex-on-discord's context).
- Can't evolve to multi-instance because the tenancy concept isn't first-class.

Get it right and we get all three: isolation, selective sharing, and future-proof.
