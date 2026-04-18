---
title: Thoughts
section: 04-data-model
tags: [data-model, schema, section/data-model, status/complete, thoughts, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[04-data-model/index]]"
reviewed: false
---
# Thoughts

Durable, inter-presence messages. Preserved from the POC, unchanged in spirit. Not memory per se — closer to a persistent notification / mailbox.

## Use cases

- `scheduler → eric/*`: "Synthesis run completed; 3 concepts promoted."
- `claude-code → eric/claude-desktop`: "Noted that you restarted the LiveKit agent; relevant logs saved as artifact X."
- `lifecycle-worker → all`: "Daily reflection digest at vault/reflections/2026-04-17.md."
- `eric/livekit-voice → eric/claude-code`: "Reminder I'll continue this discussion later via chat."

Thoughts are **not** the conversation transcript — they are targeted messages between presences that survive past their session.

## Pydantic model

```python
# musubi/types/thought.py

class Thought(BaseModel):
    object_id: KSUID
    namespace: str                      # by convention: always the from_presence's namespace
    schema_version: int = 1

    content: str = Field(min_length=1, max_length=4000)
    from_presence: str
    to_presence: str                    # concrete presence OR "all"
    channel: str = "default"            # named channel, arbitrary string

    importance: int = Field(default=5, ge=1, le=10)
    tags: list[str] = Field(default_factory=list)

    # Read tracking
    read: bool = False                  # global flag: for unicast, true when recipient reads
    read_by: list[str] = Field(default_factory=list)  # per-presence: always appended on read

    created_at: datetime
    created_epoch: float

    # Lineage (rare, but supported)
    in_reply_to: KSUID | None = None
    supersedes: list[KSUID] = Field(default_factory=list)
```

## Qdrant layout

Collection: `musubi_thought`.

Indexes: `namespace`, `object_id`, `from_presence`, `to_presence`, `channel`, `read`, `read_by`, `created_epoch`, `importance`.

## Behavior

### `thought_send`

- Creates a Thought with `read=False`, `read_by=[]`.
- Embedding is optional — we embed for semantic `thought_history` queries but sending does not require a hot-path embedding wait. An async post-write embed happens if under load.

### `thought_check`

Returns unread thoughts for `my_presence`. Filter in Qdrant:

```
must:
  to_presence IN [my_presence, "all"]
  from_presence NOT = my_presence       (don't return your own sends)
must_not:
  read_by CONTAINS my_presence           (per-presence read state)
```

For unicast (`to_presence != "all"`), the global `read` flag is also an acceptable signal — both are maintained for backward compat.

### `thought_read`

For each thought in the list:
- Append `my_presence` to `read_by` (idempotent set semantics).
- If `to_presence == my_presence` (unicast), also set `read = True`.

Batched via `batch_update_points`.

### `thought_history`

Semantic search across thoughts for a given presence (as from or to), optionally filtered by channel.

## Channel conventions

- `default` — normal messages.
- `scheduler` — automated digest + notification.
- `ops-alerts` — system alerts (degradation, failures).
- `mentions` — when a thought in another channel @mentions a presence, a copy goes here.
- Arbitrary custom channels allowed.

Channel filtering is done in query, not storage. We don't partition collections by channel.

## Isolation

Thoughts follow the standard namespace rules, with one subtlety: a thought sent from `eric/claude-code` to `eric/livekit-voice` is stored with `namespace: eric/claude-code/thought`. The recipient reads it via a query that filters by `to_presence: livekit-voice` — which requires the query to span *namespaces* within the same tenant.

Cross-tenant thoughts are allowed but require the token to carry scope for both tenants. Logged in audit.

## Test contract

**Module under test:** `musubi/planes/thoughts/` (direct port of POC `musubi/thoughts.py` with schema upgrades)

Preserved from POC tests:

1. `test_thought_send_creates_unread`
2. `test_thought_check_returns_unread_only`
3. `test_thought_check_excludes_self_sends`
4. `test_thought_check_includes_broadcast_to_all`
5. `test_thought_read_unicast_sets_read_true`
6. `test_thought_read_broadcast_appends_to_read_by_only`
7. `test_thought_read_idempotent`
8. `test_thought_read_batched_not_N_plus_1`
9. `test_thought_history_semantic_match`
10. `test_thought_history_filters_by_presence`

New for v1:

11. `test_thought_channel_filter_applies`
12. `test_thought_importance_filter_applies`
13. `test_thought_in_reply_to_chain_queries_correctly`
14. `test_thought_namespace_isolation`
15. `test_cross_tenant_thought_requires_multi_tenant_scope`
16. `test_thought_embedding_deferred_under_load_does_not_block_send`

## Migration from POC

The POC collection `musubi_thoughts` becomes `musubi_thought` (singular; consistent with other collections). Migration:

1. Create new collection with named vectors + updated index set.
2. Read POC thoughts, map old payload → new model (set `channel = "default"`, `namespace = from_presence inferred to default tenant`).
3. Re-embed with named dense + sparse (or copy old dense into `dense_legacy_v0` named vector; see [[11-migration/re-embedding]]).
4. Alias `musubi_thoughts` → `musubi_thought`.

See [[11-migration/phase-1-schema]] for the detailed migration runbook.
