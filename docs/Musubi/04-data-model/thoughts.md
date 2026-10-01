---
title: Thoughts
section: 04-data-model
tags: [data-model, schema, section/data-model, status/complete, thoughts, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[04-data-model/index]]"
reviewed: false
implements: ["src/musubi/store/specs.py", "src/musubi/types/base.py", "src/musubi/types/concept.py", "src/musubi/types/episodic.py", "src/musubi/types/lifecycle_event.py", "src/musubi/types/thought.py", "tests/test_thoughts.py", "tests/types/"]
---
# Thoughts

Durable, inter-presence messages. Not memory per se — closer to a persistent notification / mailbox.

## Use cases

- `scheduler → alex/*`: "Synthesis run completed; 3 concepts promoted."
- `claude-code → alex/claude-desktop`: "Noted that you restarted the LiveKit agent; relevant logs saved as artifact X."
- `lifecycle-worker → all`: "Daily reflection digest written."
- `alex/livekit-voice → alex/claude-code`: "Reminder I'll continue this discussion later via chat."

Thoughts are **not** the conversation transcript — they are targeted messages between presences that survive past their session.

## Pydantic model

The model is `Thought` in `src/musubi/types/thought.py:21-41`. It extends `MusubiObject` (not `MemoryObject`), so it inherits `object_id`, `namespace`, `identity_family`, `schema_version`, `created_at`/`created_epoch`, `updated_at`/`updated_epoch` and `version`, and has none of the memory lineage or reinforcement fields.

```python
class Thought(MusubiObject):
    state: Literal["provisional", "matured", "archived"] = "provisional"
    content: str = Field(min_length=1)       # no length cap on the model
    from_presence: str = Field(min_length=1)
    to_presence: str = Field(min_length=1)   # concrete presence OR "all"
    read: bool = False                       # global flag: for unicast, true when recipient reads
    read_by: list[str] = []                  # per-presence: appended on read
    channel: str = "default"                 # named channel, arbitrary string
    importance: int = Field(default=5, ge=1, le=10)

    # Lineage (rare, but supported)
    in_reply_to: KSUID | None = None
    supersedes: list[KSUID] = []
```

There is no `tags` field. `namespace` is a `<tenant>/<presence>/thought` namespace.

## Qdrant layout

Collection: `musubi_thought` (dense + sparse).

Indexes: the universal set plus `from_presence`, `to_presence`, `channel`, `read`, `read_by`, `in_reply_to` (`src/musubi/store/specs.py:214-221`).

## Behavior

### `thought_send`

- Creates a Thought with `read=False`, `read_by=[]`.
- Content is embedded (dense + sparse) on send, for semantic `thought_history` queries. `ThoughtsPlane.send(defer_embedding=True)` stores the thought without vectors; nothing embeds it later.

### `thought_check`

Returns unread thoughts for `my_presence`. Filter in Qdrant:

```
must:
  namespace = <the namespace asked for>  (exact match)
  to_presence IN [my_presence, "all"]
must_not:
  read_by CONTAINS my_presence           (per-presence read state)
  from_presence = my_presence            (don't return your own sends)
```

For unicast (`to_presence != "all"`), the global `read` flag is also an acceptable signal — both are maintained for backward compat.

### `thought_read`

For each thought in the list:
- Append `my_presence` to `read_by` (idempotent set semantics).
- If `to_presence == my_presence` (unicast), also set `read = True`.

Batched via `batch_update_points`.

### `thought_history`

Search across thoughts in one namespace, filtered by channel (default `default`), and optionally by presence, minimum importance or `in_reply_to`. With a query string it is a semantic search.

## Channel conventions

- `default` — normal messages.
- `scheduler` — automated digest + notification.
- `ops-alerts` — system alerts (degradation, failures).
- Arbitrary custom channels allowed.

Channel filtering is done in query, not storage. We don't partition collections by channel.

## Isolation

Thoughts follow the standard namespace rules. `check` and `history` match one namespace exactly (`src/musubi/planes/thoughts/plane.py:173-205`), so a sender and recipient exchange thoughts through a shared thought namespace that both tokens can read, e.g. `alex/shared/thought`; the recipient filters on `to_presence`.

Cross-tenant thoughts are not supported: the plane's `enforce_tenant_scope` flag refuses them, and no multi-tenant scope exists.

## Test Contract

**Module under test:** `src/musubi/planes/thoughts/` (tests in `tests/planes/test_thoughts.py`)

Core:

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

Filters, lineage and isolation:

11. `test_thought_channel_filter_applies`
12. `test_thought_importance_filter_applies`
13. `test_thought_in_reply_to_chain_queries_correctly`
14. `test_thought_namespace_isolation`
15. `test_cross_tenant_thought_requires_multi_tenant_scope`
16. `test_thought_embedding_deferred_under_load_does_not_block_send`
