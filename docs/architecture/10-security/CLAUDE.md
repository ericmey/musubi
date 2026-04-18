---
title: Agent Rules — Security
section: 10-security
type: index
status: complete
tags: [section/security, status/complete, type/index, agents]
updated: 2026-04-17
up: "[[10-security/index]]"
reviewed: true
---

# Agent Rules — Security (10)

Local rules for `musubi/auth/`, `musubi/redaction/`, and anything touching PII. Supplements [[CLAUDE]].

## Must

- **Every request carries a namespace.** `{tenant}/{presence}/{plane}`. Never defaulted; always explicit. Reject missing-namespace requests with `Err(AuthError)`.
- **Auth runs as middleware** in `musubi/auth/` before any business logic. Business logic never parses tokens.
- **Redact on ingest, not on retrieval.** If a PII category is in scope for this deployment, the capture path strips it before the memory is stored.
- **Audit log is append-only.** Every mutation, promotion, demotion, and login generates an audit record. Writes go to disk; reads are query-only.
- **Secrets live in 1Password + ansible-vault.** No secrets in env files committed to the repo, even `.env.example`.

## Must not

- Log full request bodies with PII. Log correlation ids and sanitized shapes.
- Introduce per-document ACLs in v1. Namespace-level is the granularity. Finer is a post-v1 discussion.
- Call an LLM with content that hasn't passed redaction (if redaction is enabled for that tenant).

## Threat model scope (v1)

- **In scope:** household-scale multi-tenant (1–5 humans), local-network access, single host, host-level disk encryption, backup exfiltration prevention.
- **Out of scope:** multi-org SaaS, per-tenant encryption keys, fine-grained RBAC, SIEM integration, formal certification.

Expanding scope requires an ADR.

## Auth shape (v1)

- Bearer tokens (opaque, 256-bit random) issued per presence.
- Optional mTLS on the LAN.
- OAuth 2.1 / dynamic client registration only on the MCP adapter (spec requires it).

## Related slices

- [[_slices/slice-auth]] — the only slice writing `musubi/auth/`.
- Redaction has no dedicated slice yet — ride inside `slice-ingestion-capture` until it grows.
