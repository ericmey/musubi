---
title: Redaction
section: 10-security
tags: [pii, privacy, redaction, section/security, security, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[10-security/index]]"
reviewed: false
---
# Redaction

Optional PII / secret redaction for captured content. Off by default; enabled per-adapter or per-namespace.

## When to use

Different presences capture different shapes of content. Sometimes what we capture contains sensitive strings that we'd rather not persist:

- LiveKit voice session — user dictates a credit card number aloud.
- OpenClaw captures a logged-in page that leaks a session cookie in the DOM.
- A coding session with Claude Code includes a `.env` file fragment.

Redaction scrubs these patterns before the content lands in Qdrant or vault.

## Default: off

Most captured content is intentionally personal. We don't want to mangle it. Redaction is opt-in:

```env
MUSUBI_LIVEKIT_REDACTION=on    # LiveKit adapter
MUSUBI_CAPTURE_REDACTION=off   # default
```

## What gets redacted

Pattern categories (configurable per-deploy):

| Category | Pattern | Replacement |
|---|---|---|
| Credit cards | Luhn-valid 13-19 digit strings | `[redacted-cc]` |
| SSN (US) | `\d{3}-?\d{2}-?\d{4}` | `[redacted-ssn]` |
| Email | RFC-5322 | `[redacted-email]` |
| Phone | North American + E.164 | `[redacted-phone]` |
| API keys | `(ghp|sk|xoxb|AKIA)_[a-zA-Z0-9]{20,}` | `[redacted-key]` |
| AWS access keys | `AKIA[0-9A-Z]{16}` | `[redacted-aws]` |
| OpenAI/Anthropic keys | `sk-[a-zA-Z0-9]{20,}` | `[redacted-llm-key]` |
| JWT tokens | `eyJ[a-zA-Z0-9._-]{20,}` | `[redacted-jwt]` |
| Private key headers | `-----BEGIN ... PRIVATE KEY-----` | (drop blob) |

More categories addable in `musubi/redaction/patterns.py`.

## When redaction runs

In the ingestion pipeline, **before** embedding:

```
capture → pydantic validation → [redaction] → dedup probe → encode → write
```

The original content is never persisted — we redact on the way in. This is deliberate; re-redacting derived data is harder.

## Exceptions per namespace

Some namespaces shouldn't ever hold even-redacted sensitive strings. Config:

```yaml
# /etc/musubi/redaction-policy.yaml
namespaces:
  "eric/livekit-voice/episodic":
    enabled: true
    categories: [credit_cards, api_keys, jwt, private_key]
    on_match: redact           # or: reject

  "eric/openclaw/episodic":
    enabled: true
    categories: [api_keys, jwt]
    on_match: redact

  "eric/claude-code/episodic":
    enabled: false             # trust; coding session is developer-authored
```

`on_match: reject` is strict — the capture call returns a structured error (`BAD_REQUEST` with `reason: sensitive_content_rejected`). Adapter can surface to user.

## Domain exclusion (capture-time)

Per-adapter, a list of URLs or domains where capture is disabled entirely. For OpenClaw:

```yaml
# OpenClaw extension settings.json
excluded_domains:
  - "*.bank.com"
  - "*.healthcare.gov"
  - "accounts.google.com"
  - "*.banking.example.com"
```

If the user highlights text on an excluded domain, the "Remember this" option is disabled. If they try the API anyway, it refuses.

Full exclusion beats redaction for banking/healthcare/auth domains — we don't want the content hitting our pipeline at all.

## LLM-pass redaction

Optional second pass: a small LLM call to detect PII that regex misses (names, addresses, rare patterns). Off by default (costs GPU time). When on:

```
capture → regex redact → llm redact → store
```

Cost: ~100ms per capture (local Qwen2.5 on 3080). Not worth it for most captures; enable per-namespace for voice or open-ended web captures.

## Keep-the-original option

For some workflows, we want the original preserved in a secure subset while the redacted version is indexed. Not supported in v1 — adds complexity. Instead: use domain exclusion or `on_match: reject`.

## Export + redaction

When the user exports ("give me everything you have"), the export carries **only** the stored (post-redaction) content. There's no shadow copy. Redacted content stays redacted; user can't recover the original. This is by design.

## False positives

Redaction is pattern-based and will sometimes over-redact — e.g., a 16-digit test string that passes Luhn. The system logs redaction events:

```json
{
  "event": "capture.redacted",
  "namespace": "...",
  "categories_matched": ["credit_cards"],
  "chars_redacted": 16,
  "object_id": "..."
}
```

User can disable categories that cause too many false positives on their workload.

## Test contract

**Module under test:** `musubi/redaction/*`

1. `test_credit_card_luhn_redacted`
2. `test_non_luhn_16_digit_not_redacted`
3. `test_api_key_redacted_but_kept_in_shape`
4. `test_private_key_pem_block_entirely_dropped`
5. `test_redaction_disabled_namespace_preserves_content`
6. `test_reject_on_match_returns_bad_request`
7. `test_redaction_runs_before_embedding`
8. `test_redacted_event_logged_with_categories`
9. `test_redaction_does_not_leak_original_in_logs`
10. `test_domain_exclusion_refuses_capture_on_excluded_host`
