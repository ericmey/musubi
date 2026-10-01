---
title: "Agent Rules — Security (10)"
section: 10-security
type: index
status: complete
tags: [section/security, status/complete, type/index, agents]
updated: 2026-10-01
up: "[[10-security/index]]"
reviewed: true
---

# Agent Rules — Security (10)

Local rules for `src/musubi/auth/` and anything touching tokens, scopes or PII. Supplements [[CLAUDE]].

## Must

- **Every request names its namespace explicitly.** `{tenant}/{name}/{plane}`, where the tenant is the agent ([[13-decisions/0030-agent-as-tenant|ADR 0030]]). Writes always name a fully qualified namespace. The one deliberate exception is `POST /v1/retrieve` without a `namespace`, which recalls across the caller's own tenant, filtered by scope ([[10-security/auth]]).
- **Auth runs before business logic,** through `src/musubi/auth/` (`tokens.py` validates, `scopes.py` decides, `middleware.py` is the request helper). Business logic never parses tokens.
- **Core does not redact content.** Redaction, where it exists, runs in the adapter before capture (`musubi-livekit` has a small `redact_pii` pass). A Core pipeline is a proposal; see [[10-security/redaction]].
- **Every scope decision is audited** as an `auth.allow` / `auth.deny` structured log event (`src/musubi/auth/scopes.py:239-257`). Every state change is recorded as a `LifecycleEvent`. There is no login, so there are no login records.
- **Secrets live in the operator's secret manager** and reach the containers as environment variables. Never commit them, not even in `.env.example` (placeholders only).

## Must not

- Log full request bodies with PII. Log correlation ids and sanitized shapes.
- Introduce per-document ACLs in v1. Namespace-level is the granularity. Finer is a post-v1 discussion.
- Concatenate captured content into an LLM instruction. Use `src/musubi/llm/prompt_boundary.py`; see [[10-security/prompt-hygiene]].

## Threat model scope (v1)

- **In scope:** a small team or single operator (1–5 humans), local-network access, single host, host-level disk encryption, backup exfiltration prevention. Not hardened for anonymous internet use.
- **Out of scope:** multi-org SaaS, per-tenant encryption keys, fine-grained RBAC, SIEM integration, formal certification.

Expanding scope requires an ADR.

## Auth shape (v1)

- JWT bearer tokens, one per presence: HS256 with the shared `JWT_SIGNING_KEY`, or RS256 verified against the issuer's JWKS (`src/musubi/auth/tokens.py:19`).
- `sub` must equal `presence`, a concrete `tenant/name` (`tokens.py:217-226`).
- Core issues no tokens and has no refresh or revocation; see [[10-security/auth]].
