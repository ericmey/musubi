---
title: Audit
section: 10-security
tags: [audit, logs, section/security, security, status/research-needed, type/spec]
type: spec
status: research-needed
updated: 2026-10-01
up: "[[10-security/index]]"
reviewed: false
---
# Audit

What Musubi records about access and change, and where to find it.

## Two audit tracks

### Auth / access audit

Every scope decision, allow or deny, is one structured log record
(`src/musubi/auth/scopes.py:239-257`). There is no separate audit file: the records
go to the process's standard log stream as JSON lines, alongside every other log
line, on the logger `musubi.auth.scopes`.

Allow:

```json
{
  "ts": "2026-10-01T10:21:34.512Z",
  "level": "info",
  "service": "musubi.auth.scopes",
  "msg": "auth.allow",
  "request_id": "abc-123",
  "event": "auth.allow",
  "sub": "alex/claude-code",
  "namespace": "alex/claude-code/episodic",
  "access": "w",
  "scope_used": "alex/claude-code/episodic:rw"
}
```

Deny:

```json
{
  "ts": "...",
  "level": "info",
  "service": "musubi.auth.scopes",
  "msg": "auth.deny",
  "request_id": "...",
  "event": "auth.deny",
  "sub": "alex/claude-code",
  "namespace": "alex/voice/episodic",
  "access": "w",
  "scope_used": null,
  "reason": "namespace 'alex/voice/episodic' not in token scope for 'w' access"
}
```

The audit fields are `event`, `sub`, `namespace`, `access`, `scope_used` and, on
denials, `reason`. `ts`, `level`, `service`, `msg` and `request_id` come from the
JSON log formatter (`src/musubi/observability/logging_setup.py`). Operator checks log
`namespace: "operator"`. Token validation failures (401s) are not audit events.
Retrieval without a namespace drops unreadable candidates without logging a denial
for each one ([[10-security/auth]]).

### Data-change audit

Every state change of canonical data records a `LifecycleEvent`
(`src/musubi/types/lifecycle_event.py`) in the `lifecycle_events` table of the
lifecycle SQLite ledger (`lifecycle/work.sqlite`; see [[06-ingestion/lifecycle-engine]]).
That table is the data audit log.

Each event captures:

- `event_id`, `object_id`, `object_type`, `namespace`.
- `from_state`, `to_state`.
- `actor`: the presence or system identifier that triggered the transition.
- `reason` (free-form string).
- `occurred_at` / `occurred_epoch`.
- `correlation_id` (the request id; empty for background jobs).
- `lineage_changes`, e.g. the links a promotion adds.

Because state changes go through the lifecycle engine, this table is the timeline of
any row.

## Retention

Core does not rotate or expire either track.

| Log | Retention |
|---|---|
| Auth audit events | Whatever the operator's log pipeline keeps (container log driver, Loki, …) |
| Reverse-proxy access log (if any) | Whatever the proxy is configured for |
| `lifecycle_events` | Kept until the operator prunes it; included in the SQLite backup |

## Access to audit logs

- Auth audit events: whoever can read the container logs on the host or in the log
  pipeline. Restrict that the same way you restrict host access.
- Lifecycle events: `GET /v1/lifecycle/events?namespace=<ns>` requires the `operator`
  scope (`src/musubi/api/routers/lifecycle.py:32-60`). It reads the Qdrant mirror
  collection `musubi_lifecycle_events`, which is declared but **not populated yet**,
  so today it returns an empty list. Without a `namespace` it always returns an
  empty list. `GET /v1/lifecycle/events/{object_id}` is a stub that returns an empty
  list (`lifecycle.py:65-73`). Until the mirror lands, query the `lifecycle_events`
  table in the SQLite ledger directly.

No user-facing audit API: small-team scope; the operator reads on demand.

## Tamper resistance

None beyond host file permissions. If you need stronger guarantees, ship logs off the
host as they are written (to a separate log store) and keep backups of the ledger on
immutable storage. Not in v1.

## Audit queries

```bash
# All denials (container logs, JSON lines):
docker compose logs core --no-log-prefix \
  | jq -c 'select(.event? == "auth.deny")'

# Writes allowed into one namespace:
docker compose logs core --no-log-prefix \
  | jq -c 'select(.event? == "auth.allow" and .namespace == "alex/claude-code/episodic" and .access == "w")'

# What happened to object X? (read the ledger directly)
sqlite3 /path/to/lifecycle/work.sqlite \
  "SELECT payload FROM lifecycle_events WHERE object_id = '<object-id>' ORDER BY occurred_epoch"
```

## Privacy of audit logs

Audit records contain:

- Namespaces (not content).
- Object IDs (not content).
- Subjects / presences.
- Reasons.

They don't contain captured content or tokens. This is deliberate: audit logs are
less sensitive than the data itself, so retention and exposure can be more permissive
without leaking personal data.

## Test contract

**Module under test:** audit paths in `src/musubi/auth/*` + `src/musubi/lifecycle/*`

1. `test_every_auth_decision_emits_audit_line` (in `tests/auth/test_auth.py`)
2. `test_audit_line_structured_json`
3. `test_audit_never_contains_content_or_token`
4. `test_lifecycle_event_per_state_transition`
5. `test_no_state_transition_without_event` (invariant check)
6. `test_operator_endpoint_returns_events_with_filters` (blocked until the Qdrant mirror is populated)
