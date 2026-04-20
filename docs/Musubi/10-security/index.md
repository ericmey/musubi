---
title: Security
section: 10-security
tags: [index, section/security, security, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# Security

Threat model, auth, redaction, data handling. Scoped to v1 — household + a few agent presences, single host, dedicated box.

## Threat model

### In scope

- **Accidental cross-namespace access.** A presence has a bug or a misconfigured token and writes to or reads from the wrong namespace.
- **Token leakage.** A token gets logged, committed, or exposed via error path.
- **Compromised agent.** An adversary gets code execution on an agent's host (laptop, phone, etc.). They can use whatever tokens that agent had.
- **Silent mutation of canonical data.** Someone (human or agent) edits Qdrant directly or patches vault frontmatter in-place, bypassing the write-log.
- **Prompt injection inside captured text.** A captured web page includes instructions aimed at the agent; those must not become commands on the Musubi side.
- **Supply chain.** Compromised model weights, compromised Python dependencies.

### Out of scope (v1)

- **Nation-state adversary on the LAN.** Single-operator, dedicated box; assumed physically trustworthy.
- **Multi-tenant isolation.** v1 is one tenant logically (Eric's household). Scopes exist but we don't harden against adversarial co-tenants.
- **DDoS resilience at scale.** Rate limits at Kong suffice for household scope.
- **HSM / signing hardware.** JWT signing key is on disk.

## Docs in this section

- [[10-security/auth]] — OAuth 2.1, token scopes, validation.
- [[10-security/redaction]] — PII handling, optional redaction pipelines, exclusion rules.
- [[10-security/data-handling]] — Encryption at rest, secrets, backups.
- [[10-security/audit]] — Audit trail, what's logged, how long.
- [[10-security/prompt-hygiene]] — Prompt injection defense, content sanitization for LLM inputs.

## Principles

1. **Least privilege via scopes.** Every token lists the exact namespaces it can read/write. Mismatches are 403 with a structured error.
2. **One canonical writer per row.** No silent mutation. Every state change emits a `LifecycleEvent`.
3. **Tokens are short-lived.** Access tokens 1h; refresh tokens 30d with rotation.
4. **Nothing sensitive in logs.** Tokens redacted at the log boundary; content limited to first 60 chars.
5. **User always has an export.** "I want all my data as a tarball" is supported via a single CLI command. See [[10-security/data-handling#export]].
6. **Prompt inputs are data, not instructions.** When we feed captured content to an LLM (synthesis, rendering), we structure it as explicitly quoted data — never concatenate raw captured text into a prompt that says "do what follows".

## Summary of controls

| Risk | Control |
|---|---|
| Cross-namespace access | Token scope check on every call |
| Privilege escalation | Operator scope is separate; normal tokens can't self-upgrade |
| Lost/leaked token | Revoke via CLI; signing key rotation |
| Host compromise | Encrypted backups; 1Password holds crown jewels |
| In-transit exposure | TLS everywhere via Kong; internal traffic optional plaintext on loopback |
| Prompt injection | LLM inputs quoted; no user-controlled system prompt |
| Supply chain | Images pinned by digest; weights pinned by checksum |
| Data exfiltration by agent | Namespace scope + outgoing rate limits |

## Not a security boundary

We want to be clear about what **isn't** defended in v1:

- Adapter processes running on an agent host. If your laptop is compromised, its tokens are compromised.
- Obsidian plugins running in the vault editor. They can read/write the full vault; we don't sandbox them.
- Voice sessions recorded by LiveKit before they reach Musubi. Upstream security is LiveKit's problem; we just redact on ingest when configured.

We call these out so expectations match reality.
