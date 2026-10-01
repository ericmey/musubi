---
title: Data Handling
section: 10-security
tags: [data, encryption, export, secrets, section/security, security, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[10-security/index]]"
reviewed: false
implements: "docs/Musubi/10-security/"
---
# Data Handling

Where data lives, how it's protected at rest, how it moves, and how to get it back
out or delete it.

## Data at rest

Musubi does not encrypt data at the application layer. **Host full-disk encryption
is the recommended at-rest control** (for example LUKS on Linux, unlocked at boot
by TPM or passphrase). Application-level encryption would cost search performance
without adding much over disk encryption for this threat model.

### Qdrant

Vectors and payloads live in Qdrant's storage directory (the `qdrant-storage`
volume in the Compose deployment). Qdrant does not encrypt at rest; rely on disk
encryption.

### Vault

Curated knowledge is plain Markdown on disk (the `vault` volume). Musubi does not
push the vault anywhere. If you keep it in git or sync it elsewhere, that remote's
access control and encryption are yours to choose; `git-crypt` is an option for
subfolders you want encrypted in the remote.

### Artifact blobs

Content-addressed files (filename = SHA-256 of the blob), so no metadata leaks via
the filename.

### SQLite

`lifecycle/work.sqlite` holds the lifecycle ledger, including the
`lifecycle_events` audit table. Next to it, `vault-writelog.db` records recent
Core writes to the vault (file path, body hash, writer) so they are not re-indexed
as human edits. Low sensitivity, but covered by disk encryption like everything
else.

### Backups

`deploy/backup/musubi-backup.sh` is a reference host-local backup driver, run by a
systemd timer (`deploy/backup/systemd/`) every six hours. Each run writes a
timestamped directory with Qdrant collection snapshots plus `SHA256SUMS`, a SQLite
`.backup` copy of `work.sqlite`, an rsync mirror of the artifact blobs, and a
`manifest.json`. Old runs are pruned after 14 days, and only after a green run.
See `deploy/backup/README.md`.

The script does not back up the vault, and it keeps everything on the same host.
Off-site copies (for example restic to object storage, with the repository
password held in your secret manager) are the operator's choice and are not
configured by Musubi.

## Data in transit

- Client → reverse proxy: TLS, terminated at an operator-provided reverse proxy.
- Reverse proxy → Core: plain HTTP on the same host. Core binds `127.0.0.1:8100` by
  default.
- Core → Qdrant / TEI / Ollama: plain HTTP on the Compose network.
- Core → identity provider: HTTPS fetch of `<OAUTH_AUTHORITY>/.well-known/jwks.json`,
  for RS256 tokens only.

Traffic that stays on the host is plaintext; the host boundary is where TLS
matters. If any service moves to a second host, that cross-host hop must use TLS.

## Secrets

| Secret | Where Core reads it | Rotation |
|---|---|---|
| JWT signing key | `JWT_SIGNING_KEY` environment variable (`src/musubi/settings.py:277`) | Manual. Rotating it invalidates every HS256 token at once; see [[10-security/auth]] |
| Qdrant API key | `QDRANT_API_KEY` environment variable | With your deploy rotation |
| Lifecycle LLM API key (only for an OpenAI-compatible backend) | `LIFECYCLE_LLM_API_KEY` environment variable | Per provider |
| Backup credentials (off-site, if any) | Wherever your backup tool reads them | Per your backup tool |

Keep every secret in your own secret manager and inject it as an environment
variable at container start. Never commit a real value to the repository.
**Your secret manager is the root of trust:** if the host is wiped, it is what
still holds the keys you need to recover, so back it up independently of the
Musubi host.

## Content handling

### What we persist

- Captured memory content (as sent by the client; Core does not redact).
- Curated Markdown body.
- Synthesized concept content.
- Artifact blob + metadata + chunks.
- Thoughts.
- Lifecycle events.

### What we explicitly do NOT persist

- Raw tokens. JWT-shaped strings are scrubbed from log messages before emit
  (`src/musubi/observability/logging_setup.py`).
- Embedding vectors in logs.

## Export ("I want my data")

**Planned, not implemented.** There is no single export command today. What exists:

- The vault is plain Markdown with frontmatter; copy the `vault` volume.
- Every plane is readable over the API with an appropriately scoped token
  ([[07-interfaces/canonical-api]]).
- The backup directories above contain Qdrant snapshots, the SQLite ledger and the
  artifact blobs.

Derived data (embeddings, HNSW index, sparse vectors) is rebuildable from the
canonical content and is not something an export needs to carry.

## Delete ("I want this data gone")

- **Soft delete** of one episodic memory, curated document or concept:
  `DELETE /v1/{episodic,curated,concepts}/{id}?namespace=<ns>` with `w` scope. The
  row transitions to `archived` through the lifecycle engine (a `LifecycleEvent` is
  recorded); the point stays in Qdrant.
- **Hard delete** of one episodic memory: `DELETE /v1/episodic/{id}?namespace=<ns>&hard=true`
  with the `operator` scope. The point is removed from Qdrant
  (`src/musubi/api/routers/writes_episodic.py:544-600`).
- **Artifact purge:** `POST /v1/artifacts/{id}/purge?namespace=<ns>` with the
  `operator` scope (`src/musubi/api/routers/writes_artifact.py:179-207`).
- **Vault document:** delete the file. The lifecycle worker's `vault_reconcile` job
  (every six hours) archives the curated row whose file is gone
  (`src/musubi/vault/reconciler.py`).
- **Whole namespace:** no endpoint or command exists. Planned, not implemented.

## GDPR / CCPA parallels

Musubi is self-hosted software, not a service. Whoever runs it is the data
controller for what it stores. The principles still map onto what exists:

- **Right to access** → read endpoints and the vault; a single export is planned.
- **Right to delete** → the delete and purge endpoints above.
- **Right to rectify** → edit the vault, or the `PATCH` endpoints.
- **Right to portability** → open formats (Markdown, JSON over HTTP).

## Incident handling

If a token or secret leaks:

1. Rotate the affected key. For HS256 tokens, change `JWT_SIGNING_KEY` and restart
   Core; for RS256, revoke the key at your identity provider. There is no per-token
   revocation.
2. Re-mint the tokens you still need.
3. Review `auth.allow` / `auth.deny` events for anomalous use while the leak was live
   ([[10-security/audit]]).
4. If a namespace was accessed improperly, cross-check the lifecycle events for
   changes in that namespace.
5. If the host or its disk keys are compromised, re-provision and restore from a
   backup taken before the compromise.

## Test Contract

**Module under test:** data handling policies

Existing tests that cover this page's behaviour:

1. `tests/observability/test_observability.py::test_redact_token_filter_idempotent`
2. `tests/api/test_api_v0_write.py::test_hard_delete_removes_an_identity_damaged_row_via_http`
3. `tests/api/test_api_v0_write.py::test_artifact_purge_requires_operator`
4. `tests/api/test_api_v0_write.py::test_artifact_purge_truthful_and_idempotent_and_fenced`

Not yet covered: a single export command (planned) and namespace-wide purge
(planned).
