---
title: Redaction
section: 10-security
tags: [pii, privacy, redaction, section/security, security, status/research-needed, type/spec]
type: spec
status: research-needed
updated: 2026-10-01
up: "[[10-security/index]]"
reviewed: false
implements: "docs/Musubi/10-security/"
---
# Redaction

PII and secret redaction for captured content.

## What exists today

**Core has no redaction pipeline.** `POST /v1/episodic` and the other capture
routes store content as the client sends it. If content must be scrubbed, the
client has to do it before capture.

The one implemented redactor lives in the external
[`musubi-livekit`](https://github.com/sourceblender/musubi-livekit) adapter: an
opt-in `redact_pii` pass (enabled with `redact_pii=True` in its adapter config)
that replaces email addresses, US SSN-shaped numbers and 10-digit phone numbers in
voice transcripts with `[REDACTED]` before capture. Core keeps a compatibility
import at `src/musubi/adapters/livekit/redaction.py`, which only works when
`musubi-livekit` is installed. See that repository for its configuration.

Separately, Core scrubs JWT-shaped strings from its own **log messages**
(`src/musubi/observability/logging_setup.py`). That protects logs, not stored
content.

The rest of this page is a **proposal, not implemented**.

## Why it is off by default

Different presences capture different shapes of content, and sometimes that
content contains strings we would rather not persist:

- a voice session where someone reads a card number aloud;
- a browser capture of a page that shows a session token;
- a coding session that includes an `.env` fragment.

But captured content is free-form, and mangling it hurts recall. Redaction should
stay opt-in, per adapter or per namespace.

## Proposed: Core redaction pipeline (planned, not implemented)

Pattern categories, configurable per deployment:

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

It would run in the capture path **before** embedding, so the original is never
persisted:

```
capture → pydantic validation → [redaction] → dedup probe → encode → write
```

Per-namespace policy would choose the categories and the action on a match:
`redact` (replace and store) or `reject` (refuse the capture with a `BAD_REQUEST`
error the adapter can show to the user). Some namespaces (a coding session the
developer authored) would leave it off.

Each redaction would emit a structured log event (namespace, categories matched,
characters redacted, object id; never the original text) so over-redaction can be
spotted and a noisy category switched off.

### Domain exclusion

For browser adapters, excluding whole domains (banking, health, sign-in pages) at
capture time beats redaction: the content never reaches Musubi. That belongs in the
adapter's own settings; see the adapter's repository.

### LLM-pass redaction

An optional second pass with the configured local LLM to catch PII that regex
misses (names, addresses). It costs GPU time per capture, so it would be enabled
per namespace, for voice or open-ended web captures only:

```
capture → regex redact → llm redact → store
```

### Keeping the original

Not planned. Preserving an unredacted copy beside the redacted one adds a second,
more sensitive store. Use domain exclusion or `reject` instead.

## Test Contract

**Module under test:** the proposed Core redaction module (does not exist yet)

1. `test_credit_card_luhn_redacted`
2. `test_non_luhn_16_digit_not_redacted`
3. `test_api_key_redacted_but_kept_in_shape`
4. `test_private_key_pem_block_entirely_dropped`
5. `test_redaction_disabled_namespace_preserves_content`
6. `test_reject_on_match_returns_bad_request`
7. `test_redaction_runs_before_embedding`
8. `test_redacted_event_logged_with_categories`
9. `test_redaction_does_not_leak_original_in_logs`
