---
title: Security
section: 10-security
tags: [index, section/security, security, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[00-index/index]]"
reviewed: false
---
# Security

Threat model, auth, redaction, data handling. Scoped to v1: a small team or single operator (1–5 humans) and a few agent presences on a single host. Musubi is not hardened for anonymous internet use.

## Threat model

### In scope

- **Accidental cross-namespace access.** A presence has a bug or a misconfigured token and writes to or reads from the wrong namespace.
- **Token leakage.** A token gets logged, committed, or exposed via error path.
- **Compromised agent.** An adversary gets code execution on an agent's host (laptop, phone, etc.). They can use whatever tokens that agent had.
- **Silent mutation of canonical data.** Someone (human or agent) edits Qdrant directly or patches vault frontmatter in-place, bypassing the write-log.
- **Prompt injection inside captured text.** A captured web page includes instructions aimed at the agent; those must not become commands on the Musubi side.
- **Supply chain.** Compromised model weights, compromised Python dependencies.

### Out of scope (v1)

- **Adversaries on the local network.** Musubi assumes a trusted network segment and a physically trustworthy host.
- **Multi-tenant isolation.** v1 assumes a single trusted team. Tenants are agents and scopes separate them, but we don't harden against adversarial co-tenants.
- **DDoS resilience at scale.** Core's per-token write rate limits (`src/musubi/api/rate_limit.py`) are sized for small-team use, not for absorbing an attack.
- **HSM / signing hardware.** The HS256 signing key is an environment secret on the host.

## Docs in this section

- [[10-security/auth]] — JWT bearer tokens, scopes, validation.
- [[10-security/redaction]] — PII handling: what exists today (adapter-side) and the proposed Core pipeline.
- [[10-security/data-handling]] — Encryption at rest, secrets, backups.
- [[10-security/audit]] — Audit trail, what's logged, how long.
- [[10-security/prompt-hygiene]] — Prompt injection defense, content sanitization for LLM inputs.

## Principles

1. **Least privilege via scopes.** Every token lists the exact namespaces it can read/write. Mismatches are 403 with a structured error.
2. **One canonical writer per row.** No silent mutation. Every state change emits a `LifecycleEvent`.
3. **Tokens should be short-lived.** The issuer chooses `exp`; Core verifies it when present but does not require it, and there are no refresh tokens. Set an `exp` on every token, shorter for operator tokens.
4. **Nothing sensitive in logs.** The JSON log formatter scrubs JWT-shaped strings from log messages before emit (`src/musubi/observability/logging_setup.py`). Auth audit events carry namespaces and subjects, not content.
5. **Data stays in open formats.** Curated knowledge is plain Markdown in the vault, and every plane is readable over the API. A single export command is planned, not implemented; see [[10-security/data-handling]].
6. **Prompt inputs are data, not instructions.** When we feed captured content to an LLM (synthesis, rendering), we structure it as explicitly quoted data — never concatenate raw captured text into a prompt that says "do what follows".

## Summary of controls

| Risk | Control |
|---|---|
| Cross-namespace access | Token scope check on every call |
| Privilege escalation | Operator scope is separate; normal tokens can't self-upgrade |
| Lost/leaked token | Rotate the signing key (no per-token revocation); short `exp` |
| Host compromise | Full-disk encryption; secrets held in the operator's secret manager, not in the repo |
| In-transit exposure | TLS terminated at an operator-provided reverse proxy; Core binds `127.0.0.1` by default; container-to-container traffic is plaintext on the Compose network |
| Prompt injection | LLM inputs quoted; no user-controlled system prompt |
| Supply chain | Images pinned by digest in the deploy files |
| Data exfiltration by agent | Namespace scope + per-token write rate limits |

## Not a security boundary

We want to be clear about what **isn't** defended in v1:

- Adapter processes running on an agent host. If your laptop is compromised, its tokens are compromised.
- Obsidian plugins running in the vault editor. They can read/write the full vault; we don't sandbox them.
- Voice sessions recorded by LiveKit before they reach Musubi. Upstream security is LiveKit's problem; the `musubi-livekit` adapter can redact a few PII shapes before capture when configured. Core itself does not redact.

We call these out so expectations match reality.
