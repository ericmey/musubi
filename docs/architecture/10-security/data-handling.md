---
title: Data Handling
section: 10-security
tags: [data, encryption, export, secrets, section/security, security, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[10-security/index]]"
reviewed: false
implements: "docs/architecture/10-security/"
---
# Data Handling

Where data lives, how it's protected at rest, how it moves, and how the user gets it back out if they want to leave.

## Data at rest

### Qdrant

Storage directory (`/var/lib/musubi/qdrant/`) lives on NVMe. For v1, **disk-level encryption** is the layer of defense:

- LUKS full-disk encryption on the NVMe (set up during Ubuntu install; key unlocked at boot via TPM or passphrase).
- Qdrant itself doesn't encrypt at rest in 1.15; application-level encryption would hurt search performance without adding much over LUKS for our threat model.

### Vault

Markdown files on disk, same LUKS-encrypted NVMe. Sync'd to git remote — **git repo is private** and **access-controlled via deploy key**. Additional layer:

- Optional: `git-crypt` for specific subfolders (e.g., `vault/private/`) transparently encrypting those files in git.
- Not applied by default; most content is intentionally ordinary.

### Artifact blobs

On LUKS-encrypted NVMe. Content-addressed (filename = SHA-256 of blob) — no metadata leaks via filename.

### Sqlite

`lifecycle-work.sqlite` on LUKS-encrypted disk. Contains write-log entries (path + hash + presence) — low sensitivity, but covered by LUKS.

### Backups

- **Qdrant snapshots** → `/mnt/snapshots/` → SATA SSD (also LUKS).
- **Vault git** → GitHub private repo (TLS in transit; GitHub's at-rest encryption; optional `git-crypt` for subsets).
- **sqlite backups** → SATA.
- **Off-site (restic)** → encrypted with repository password held in 1Password. `AES-256` via restic's built-in crypto. The repo password is independent of LUKS keys — catastrophic LUKS key loss doesn't affect off-site recovery.

## Data in transit

- Adapter → Kong: TLS 1.3 (Let's Encrypt or internal CA).
- Kong → Core: localhost HTTP (inside same host; loopback interface).
- Core → Qdrant / TEI / Ollama: Docker network, HTTP (inside host).
- Core → Auth authority: HTTPS (same host, but still TLS).
- Core → GitHub (git push): SSH with deploy key, ED25519.
- Core → off-site backup: SSH or HTTPS per restic config.

LAN loopback traffic is plaintext; the host boundary is where TLS matters. If we ever add a second host (see [[11-migration/scaling]]), cross-host traffic must be TLS.

## Secrets

| Secret | Location | Rotation |
|---|---|---|
| JWT signing key | `/etc/musubi/jwt-signing-key.pem` (0400) | Manual; every ~180d |
| Qdrant API key | `/etc/musubi/.env` | With every deploy rotation |
| OAuth signing key | Auth authority's keystore | Per auth authority docs |
| Vault git deploy key | `/home/musubi/.ssh/id_ed25519` (0600) | Yearly |
| Restic repo password | 1Password | Yearly |
| Optional: CF API token | `/etc/musubi/.env` (TLS cert issuance) | As needed |
| GEMINI_API_KEY (if used) | `/etc/musubi/.env` | Per-provider |

**1Password is the root of trust.** If the host is wiped, 1Password still holds the restic password (off-site backup recovery) and SSH deploy keys (vault git re-clone). Losing 1Password access is catastrophic; it's backed up by Apple Keychain / the user's own backup strategy.

## Content handling

### What we persist

- Captured memory content (post-redaction, if enabled).
- Curated Markdown body.
- Synthesized concept content.
- Artifact blob + metadata + chunks.
- Thoughts.
- Lifecycle events.

### What we explicitly do NOT persist

- Raw tokens (only the hash or presence mapping).
- Request/response bodies in access logs (only status + timing).
- Embedding vectors in logs.
- Gemini / Anthropic API responses (if we add hosted LLM calls later — cache in memory, not on disk).

## Export ("I want my data")

```
musubi-cli export --output ~/musubi-export-$(date +%F).tar.gz
```

Produces a tarball containing:

- `vault/` — full vault Markdown + frontmatter.
- `artifacts/` — blobs by content hash + `artifacts.jsonl` metadata.
- `memories.jsonl` — all episodic memories (current content).
- `concepts.jsonl` — all concepts.
- `thoughts.jsonl` — all thoughts.
- `manifest.json` — counts, range, signing.

Plus a `README.md` explaining the format.

Size: depends on artifact content. ~100 MB / 10k memories is typical.

No export of derived data (embeddings, HNSW index, sparse vectors) — they're not yours in a meaningful sense; they're indexable from the canonical data.

Format is stable across minor versions; breaking schema changes bump a `manifest.format_version`.

## Delete ("I want this data gone")

- Single memory / concept / thought: `DELETE /v1/<resource>/<id>` → state=`archived`, still visible to operator.
- Hard delete: `POST /v1/<resource>/<id>/purge` → operator-only, removes from Qdrant.
- Whole namespace: `musubi-cli purge-namespace <ns>` → operator-only, irreversible.
- Vault doc: delete the file in Obsidian → Watcher archives in Qdrant.

Hard-delete is asynchronous to Qdrant's segment optimization; the row is gone for queries immediately, and the vectors are pruned on the next optimizer pass.

## Leaving Musubi

"I want to migrate off" — export → tarball → import elsewhere. The export format is documented; re-imports into a clean Musubi must round-trip.

We don't build migration tools to other products (Obsidian Copilot, Mem0 cloud, etc.) — but the export is structured enough that writing a converter is a weekend's work.

## GDPR / CCPA parallels

v1 isn't a service, so compliance regimes technically don't apply — but the principles do, and we honor them:

- **Right to access** → `musubi-cli export`.
- **Right to delete** → hard-delete endpoints.
- **Right to rectify** → vault / PATCH endpoints.
- **Right to portability** → export format is open and documented.

If v1 ever serves multiple humans as a service, we re-evaluate.

## Incident handling

If a token or secret leaks:

1. Revoke it: `musubi-auth revoke --jti ...` or remove from client registry.
2. Rotate the affected key: see [[09-operations/runbooks#rotate-tokens]].
3. Review access logs for anomalous use while the leak was live.
4. If a namespace was accessed improperly, audit via lifecycle events + access log.
5. If LUKS/root is compromised, full re-provision + restore from off-site backup.

## Test Contract

**Module under test:** data handling policies + export

1. `test_token_never_appears_in_access_log`
2. `test_content_truncated_in_log_to_60_chars`
3. `test_export_includes_all_canonical_data`
4. `test_export_excludes_derived_vectors`
5. `test_export_manifest_contains_counts_and_version`
6. `test_hard_delete_removes_from_qdrant_on_next_optimizer_pass`
7. `test_purge_namespace_prevents_future_captures_there`
8. `test_restic_backup_encrypts_before_leaving_host` (integration)
9. `test_git_push_uses_deploy_key_not_password`
10. `test_localhost_traffic_allowed_plaintext_but_cross_host_tls_required`
