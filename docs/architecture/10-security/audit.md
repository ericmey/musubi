---
title: Audit
section: 10-security
tags: [audit, logs, section/security, security, status/research-needed, type/spec]
type: spec
status: research-needed
updated: 2026-04-17
up: "[[10-security/index]]"
reviewed: false
---
# Audit

What we log, how we find it, how long we keep it.

## Two audit tracks

### Auth / access audit

Every authorization decision — allow or deny. Lives in `/var/log/musubi/auth.log` (separate from app log so it has its own retention + access control).

Format:

```json
{
  "ts": "2026-04-17T10:21:34.512Z",
  "event": "auth.allow",
  "request_id": "abc-123",
  "sub": "eric-claude-code",
  "client_id": "musubi-mcp",
  "presence": "eric/claude-code",
  "endpoint": "POST /v1/memories",
  "namespace": "eric/claude-code/episodic",
  "scope_used": "eric/claude-code/episodic:rw",
  "source_ip": "10.0.0.5"
}
```

Deny example:

```json
{
  "ts": "...",
  "event": "auth.deny",
  "request_id": "...",
  "sub": "eric-mcp",
  "endpoint": "POST /v1/memories",
  "namespace_requested": "eric/other/episodic",
  "reason": "scope_mismatch",
  "scope_available": ["eric/claude-code/episodic:rw"]
}
```

### Data-change audit

Every mutation of canonical data → a `LifecycleEvent` row in the `lifecycle_events` table (see [[06-ingestion/lifecycle-engine]]). That's our data audit log.

Each event captures:

- `object_id` affected.
- `from_state`, `to_state`.
- `actor` (`user`, `system:maturation`, `operator`, etc.).
- `reason` (string, free-form).
- `timestamp`, `request_id`, `job_id`.
- Links to related objects (e.g., promotion links concept_id → curated_id).

Because nothing mutates silently (by design), this table is a complete audit trail. Replaying it reconstructs the timeline of any row.

## Retention

| Log | Retention |
|---|---|
| `auth.log` | 90 days |
| App log | 30 days |
| Access log (Kong) | 30 days |
| `lifecycle_events` | 180 days (configurable per-deploy; can be indefinite) |

Auth log kept longer because investigations may lag. Shorter than lifecycle because it's much higher volume.

## Access to audit logs

- Auth log: readable only by operator (file permissions `0640 musubi:ops`).
- Lifecycle events: operator-only endpoint `GET /v1/lifecycle/events` (with filters).
- App log: same as auth.

No user-facing audit API — household scope; operator reads on demand.

## Tamper resistance

For v1, file-level integrity. If we need stronger guarantees:

- Logs flush to off-host immediately (syslog over TLS to a separate box).
- Immutable storage: append-only S3 bucket or object-lock.

Not in v1.

## Audit queries

Common queries the operator runs:

```
# Who captured to namespace X in the last day?
grep '"namespace": "eric/claude-code/episodic"' /var/log/musubi/auth.log \
  | jq 'select(.event == "auth.allow" and .endpoint | startswith("POST"))'

# All denies in last 24h:
grep '"event": "auth.deny"' /var/log/musubi/auth.log \
  | jq 'select(.ts > "2026-04-16T10:00")' | head -100

# What happened to concept X?
curl -H "Authorization: Bearer $OP_TOKEN" \
  "http://localhost:8100/v1/lifecycle/events/<concept-id>" | jq .
```

## Privacy of audit logs

Audit logs contain:

- Namespaces (not content).
- Object IDs (not content).
- Subjects / presences.
- IPs.
- Reasons.

They don't contain captured content. This is deliberate — audit logs are less sensitive than the data itself, so retention + exposure can be more permissive without leaking personal data.

## Test contract

**Module under test:** audit paths in `musubi/auth/*` + `musubi/lifecycle/*`

1. `test_every_auth_decision_emits_one_audit_line`
2. `test_audit_line_structured_json`
3. `test_audit_never_contains_content_or_token`
4. `test_lifecycle_event_per_state_transition`
5. `test_no_state_transition_without_event` (invariant check)
6. `test_audit_retention_sweep_after_ttl`
7. `test_operator_endpoint_returns_events_with_filters`
