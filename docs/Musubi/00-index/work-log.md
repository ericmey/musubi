---
title: Work Log
section: 00-index
type: index
status: living-document
tags: [section/index, status/living-document, type/index]
updated: 2026-04-21
up: "[[00-index/index]]"
reviewed: true
---

# Work Log

Append-only record of infrastructure, tooling, and implementation events that
matter to Musubi. Newest first. One entry per event.

An entry earns a line here when it:

- Realises a spec (a dedicated host, service, or interface comes online).
- Flips a slice to `done` (or back to `ready`).
- Closes a [[00-index/research-questions|research question]] or an ADR.
- Invalidates a spec (something in the spec no longer reflects reality).
- Unblocks downstream work (adds an SSH key, opens a port, wires a credential).

What it is **not:** a commit log. Code commits live in git. This log is for
*vault-visible* events.

## Entries

### 2026-04-21 — Prometheus scraping musubi.example.local (P1 observability)

`musubi.example.local` now runs a Prometheus container alongside the main
stack. Scrape targets (verified via `/api/v1/query?query=up`, all
returning `1`):

| Job | Target | Exposing |
|---------------|----------------------|-------------------------------------|
| musubi-core | `core:8100/v1/ops/metrics` | HTTP request counts/duration/5xx (real, live) |
| tei-dense | `tei-dense:80/metrics` | batch latency, queue depth, GPU util |
| tei-sparse | `tei-sparse:80/metrics` | same |
| tei-reranker | `tei-reranker:80/metrics` | same |
| prometheus | `localhost:9090/metrics` | self-scrape |

Reachable via SSH tunnel: `ssh -L 9090:localhost:9090 musubi.example.local`,
then http://localhost:9090. Bound 127.0.0.1-only — Kong deferral per
ADR 0024 means no external exposure tonight.

Deviations from [[09-operations/observability]]:

- Spec claims `musubi-core:9100`. Reality: `core:8100/v1/ops/metrics`.
 (`9100` was the old slice-ops-observability plan; the actual endpoint
 landed on `core:8100` when the router was written.)
- Spec names `musubi_capture_total`, `musubi_retrieve_total`, lifecycle
 counters, etc. Code emits `musubi_http_requests_total`,
 `musubi_http_request_duration_ms`, `musubi_5xx_total`. The per-domain
 counters are aspirational — wiring them is its own piece of work.
- Spec calls for Grafana + Alertmanager. Tonight ships Prometheus only.
 Grafana + alerts are a separate slice when notification channels
 (email? ntfy?) are decided.

**Not yet scraped (documented in the config):**

- Qdrant `/metrics` returns 401 — requires the `api-key` header. Needs a
 secret-file wiring that isn't in scope tonight.
- Ollama has no native Prometheus exporter; leave as-is.
- Lifecycle worker emits metrics via `default_registry()` but doesn't
 serve HTTP — scraping it needs an embedded HTTP endpoint in the
 worker entrypoint. Follow-up.
- Node-exporter for host-level (CPU/mem/disk/GPU) metrics not deployed
 yet.

13 structural tests in [`tests/ops/test_prometheus.py`](../../../tests/ops/test_prometheus.py).

### 2026-04-21 — Host-local backup scheduler live (P1 from the first-deploy punchlist)

`musubi.example.local` now backs itself up every six hours without Ansible.
[`deploy/backup/musubi-backup.sh`](../../../deploy/backup/musubi-backup.sh) + a
systemd timer snapshot all seven Qdrant collections (`musubi_artifact`,
`musubi_artifact_chunks`, `musubi_concept`, `musubi_curated`,
`musubi_episodic`, `musubi_lifecycle_events`, `musubi_thought`), copy
`/var/lib/musubi/lifecycle/work.sqlite` via `sqlite3 .backup`, mirror
`/var/lib/musubi/artifact-blobs`, and write a manifest + SHA256SUMS under
`/var/lib/musubi/backups/<TIMESTAMP>/`. Retention is 14 days, pruned
only after a green run. First real run: ~10 MB of snapshots, clean
status=0. 18 structural tests in [`tests/ops/test_backup_scheduler.py`](../../../tests/ops/test_backup_scheduler.py).

**Why host-local rather than extending `deploy/backup/backup.yml`:** the
ansible path targets `127.0.0.1:6333` (which doesn't exist in the
compose era — Qdrant is only on the `musubi_default` bridge) and
`/mnt/snapshots/` (which doesn't exist on the current host — the VG has
0 VFree and snapshot-capable mount was never provisioned). Also: if
the ansible control host is down for maintenance, the backup path shouldn't go down with
it. The ansible playbook remains as the canonical drill procedure and
the future offsite-push path when restic + B2 creds land.

**Deployment gotchas surfaced while wiring this:**

- Qdrant writes snapshots to `/qdrant/snapshots` inside the container,
 **not** `/qdrant/storage/snapshots`. Default `snapshots_path: ./snapshots`
 is relative to the working dir (`/qdrant`), not the storage dir.
 Without a bind mount, snapshots are ephemeral container storage.
 Added `/var/lib/musubi/qdrant-snapshots:/qdrant/snapshots` to the
 compose template.
- Collection names in-store drifted from the spec's plan —
 `musubi_artifact` (not `musubi_artifact_heads`), plus
 `musubi_lifecycle_events` which was declared in `store/` but not in
 any spec section's canonical list. The backup driver queries
 `/collections` at run time and iterates whatever's there, so future
 collection renames no-op against the script.

**Still on the P1 punchlist:** Prometheus / observability scrape.
**Still on the P2 punchlist:** offsite backup tier (restic → B2) waiting
on the secrets vault; issues #150/#151 for GHCR image publish + update
workflow.

### 2026-04-21 — Lifecycle worker live; capture → retrieve round-trip closed

The final first-deploy functional gap — captures stuck in `state: provisional`
with no runner to mature them — is closed. A new
`musubi-lifecycle-worker-1` container runs `python -m musubi.lifecycle.runner`
and drives the documented cron schedule from
[[06-ingestion/lifecycle-engine]]. Maturation jobs are wired to real
implementations via `build_maturation_jobs()`; the remaining sweeps
(synthesis / promotion / demotion / reflection / vault_reconcile) stay
placeholder-lambdas until follow-up slices land real builders.

**What landed:**

- [`src/musubi/lifecycle/runner.py`](../../src/musubi/lifecycle/runner.py) —
 tick-driven asyncio scheduler. Design rationale in
 [[13-decisions/0025-lifecycle-runner-without-apscheduler]] (no
 APScheduler dependency — rebuild that later when we need
 persisted-jobstore semantics).
- [`src/musubi/llm/ollama.py`](../../src/musubi/llm/ollama.py) —
 httpx-backed `OllamaClient` satisfying the `OllamaClient` Protocol on
 [[06-ingestion/maturation]]. Returns `None` on outage (sweep falls
 back to captured values); validates response via pydantic. 17 unit
 tests, all using `pytest-httpx` — no live Ollama needed for CI.
- Prompts: `src/musubi/llm/prompts/{importance,topics}/v1.txt` —
 frozen per the rule in `docs/Musubi/06-ingestion/CLAUDE.md`.
- [`src/musubi/lifecycle/maturation.py`](../../src/musubi/lifecycle/maturation.py) —
 `default_ollama_client()` now returns the real client instead of the
 loud stub.
- [`deploy/ansible/templates/docker-compose.yml.j2`](../../../deploy/ansible/templates/docker-compose.yml.j2) —
 added `lifecycle-worker` service using the core image with a new
 entrypoint. Inherited HEALTHCHECK disabled (worker doesn't serve HTTP).

**Verification on `musubi.example.local` (2026-04-21):**

1. Rebuilt `musubi-core:dev`, transferred via `docker save | ssh docker load`.
2. Edited `/etc/musubi/docker-compose.yml` in place to add the
 `lifecycle-worker` block (Ansible is the source-of-truth but the vault
 pass lives on the ansible control host; in-place edit was the pragmatic path).
3. `docker compose up -d --force-recreate core lifecycle-worker` — both
 came up clean; compose stack still reports all healthy.
4. Worker logs show `lifecycle-runner-starting jobs=[...] tick_seconds=60`
 and `concept_maturation` firing at its documented `03:30` cron window.
5. Forced one manual `episodic_maturation_sweep` via
 `docker compose exec lifecycle-worker python -c "…"`:
 `SweepReport(selected=3, transitioned=3, enriched=2, failed=0)`.
6. `/v1/retrieve {query_text: "first deploy smoke", namespace: "eric/ops/episodic"}`
 returns the three matured captures. **Round-trip closed.**

**What the LLM did:** Qwen-3:4B on the local Ollama returned valid JSON
matching the pydantic schemas on both prompts. Two of three captures
received inferred topics (`testing/smoke`, `project/musubi` + `deployment/first-deploy`);
the third was a timestamp-only capture — topics correctly empty.

**Open items for the punchlist:**

- P1 Observability: stand up Prometheus; scrape `core:/v1/ops/metrics` and
 `lifecycle-worker` (when the metrics exporter slice lands).
- P1 Qdrant backup cron: `deploy/backup/` scripts exist but aren't
 scheduled.
- P2 [[_slices/slice-ops-core-image-publish]]: CI-published GHCR image
 + digest-pinned `group_vars`.
- P2 [[_slices/slice-ops-update-workflow]]: `deploy/ansible/update.yml`
 with `policy: always` pulls.
- P3 Real builders for the non-maturation sweeps (synthesis, promotion,
 demotion, reflection, vault_reconcile).

### 2026-04-20 — Musubi stack live on `musubi.example.local` (first real deploy)

The six-container Musubi compose stack came up end-to-end against the real
target host for the first time. All services report `healthy`:

```
musubi-core-1 healthy /v1/ops/health → {"status":"ok","version":"v0"}
musubi-ollama-1 healthy qwen3:4b loaded (per [[13-decisions/0019-qwen-on-musubi-gpu-phase-1]])
musubi-qdrant-1 healthy v1.17.1, api-key-auth on
musubi-tei-dense-1 healthy BGE-M3
musubi-tei-reranker-1 healthy BGE-reranker-v2-m3
musubi-tei-sparse-1 healthy SPLADE v3
```

GPU: 3.3 / 10 GiB VRAM used; room for query-time batching.

### Getting there surfaced a chain of real bugs in the playbooks

Every one of these was a spec-vs-reality gap that `slice-ops-first-deploy`
could not have caught because it shipped without ever running against a real
host. Each fix landed via PR tonight:

- **#146** `chore(ansible): parametrise inventory + the ansible control host control-host workflow` —
 replaced `<placeholder>` literals in `deploy/ansible/inventory.yml` with
 `{{ jinja_vars }}`, added `deploy/ansible/setup-control-host.sh` to seed
 `~/.musubi-secrets/` on the ansible control host. Musubi playbooks are now run from the ansible control host alongside
 the homelab fleet ansible; vault password is shared (`~/ansible/.vault_pass`).
- **#147** `chore(ansible): bootstrap.yml adds Docker + NVIDIA apt repos` —
 `bootstrap.yml` was missing the Docker-CE and NVIDIA container-toolkit apt
 repo setup; first `apt install` failed immediately. Driver install pattern
 changed from the rotted `nvidia-driver-560-server` pin to
 `ubuntu-drivers autoinstall` guarded by an `nvidia-smi` probe (so the
 pre-staged 580.126.20 isn't downgraded).
- **#148** `feat(ops): musubi-core Dockerfile + compose/env template fixes` —
 wrote the missing Musubi Core `Dockerfile` (was only referenced as
 `ghcr.io/example/musubi-core:<digest>` — no such image existed). Rewrote
 the `.env.production.j2` template against the real `settings.py` field
 set (it was ~half-missing). Corrected TEI image tag from `1.5-cuda`
 (doesn't exist on GHCR) to `86-1.2.0` (Ampere compute-8.6 variant). Dropped
 `--pooling rerank` from the reranker service (rerankers are auto-detected
 in TEI 1.x; only `cls / mean / splade` are valid pooling values). Decoupled
 the Ollama healthcheck from model-pull state (the old check deadlocked
 against `core.depends_on`). Moved every healthcheck off `curl` (not
 installed in the minimal Qdrant / Ollama / TEI images) to either `bash
 /dev/tcp` or the service's own CLI. Set `MUSUBI_ALLOW_PLAINTEXT=true` so
 Core talks HTTP to in-bridge Qdrant (TLS inside the compose network is
 orthogonal; Kong-deferred per ADR 0024 means Core isn't externally
 exposed either).

### One-time operator steps that happened outside the playbooks

- `ericmey`'s Mac pubkey added to the control host's `authorized_keys` (via Proxmox
 console — the prior laptop's key didn't follow).
- the control host's SSH key registered as a read-only GitHub deploy key on
 `ericmey/musubi` so the ansible control host can `git clone` the repo.
- Native `qdrant.service` / `ollama.service` / `open-webui.service` stopped,
 disabled, and purged (binaries, data dirs, service users). Design call per
 discussion: `bootstrap.yml` assumes a greenfield host going forward; the
 native services were pre-staging artefacts only.
- HF cache at `/home/ericmey/musubi-hf-cache/hub/` rsynced into
 `/var/lib/musubi/tei-models/` so SPLADE v3 loads from cache (it's gated on
 HuggingFace; download would 401). Automating this is a follow-up (the
 current `bootstrap.yml` doesn't do it).

### Vault changes

- [[08-deployment/host-profile|host-profile.md]] § *Actually deployed state*
 updated from pre-Compose snapshot (2026-04-18) to post-Compose reality.
- [[_slices/slice-ops-first-deploy|slice-ops-first-deploy.md]] work-log
 appended with the execution record and the bugs-found ledger.
- [[12-roadmap/status|status.md]] v1 progress table updated; Phase 8 Ops
 rolls over to *first deploy complete*.

### Downstream unblocked

- **First capture → retrieve round-trip** is now testable end-to-end
 (`deploy/smoke/verify.sh` against `http://10.0.0.45:8100`). Queued for
 the next session.
- **POC → v1 data migration** (`slice-poc-data-migration`) now has a live
 target Musubi it can push into.
- **OpenClaw / LiveKit / MCP adapters** have a real endpoint to integration-
 test against.

### 2026-04-20 — slice-poc-data-migration completed via SDK

The POC data migration script `poc-to-v1.py` successfully completed discovery and implementation against `control.example.local`. Connected to the local POC Qdrant source via port 6333, mapped and transformed the `musubi_memories` and `musubi_thoughts` rows with deterministic KSUIDs. 

During implementation, it was verified that the `MusubiClient.memories.capture` endpoint does not support backdating `created_at`, so `[[_inbox/cross-slice/migrator-needs-created-at-override]]` was opened for the SDK and API teams to implement an override.

Vault changes:
- [[_slices/slice-poc-data-migration]] — `status: in-progress → in-review`.
- [[11-migration/phase-1-schema]] — updated via discovery to describe the exact payload mapping constraints from the local Qdrant POC.

### 2026-04-19 — slice-adapter-mcp first cut ready for review

Implements the MCP adapter exposing the Musubi SDK interface over local `stdio` and remote `sse` transports. Preserves legacy POC tool surface (`memory_capture`, `memory_recall`, `thought_send`, etc). Authored ADR 0021 to formally adopt Anthropic's official `mcp` library. 

Vault changes:
- [[_slices/slice-adapter-mcp]] — `status: in-progress → in-review`.
- [[07-interfaces/mcp-adapter]] — updated `implements:` + location context.
- [[13-decisions/0021-mcp-server-library]] — newly authored.

### 2026-04-19 — slice-retrieval-orchestration first cut ready for review

Implements the top-level retrieval pipeline entrypoint. Dispatches queries to fast, deep, or blended mode depending on query parameters, enforces timeouts, normalizes all backend outputs into standard `RetrievalResult` objects, and returns `RetrievalError` for any validation or retrieval failures.

Vault changes:
- [[_slices/slice-retrieval-orchestration]] — `status: in-progress → in-review`.

### 2026-04-19 — slice-lifecycle-promotion first cut ready for review

Implements the promotion and demotion sweeps. Promotion renders a human-readable markdown file via an LLM and writes it to the Obsidian vault via VaultWriter, then creates a CuratedKnowledge point. Demotion decays mature episodic memories and concepts that are unreinforced or low importance.

Vault changes:
- [[_slices/slice-lifecycle-promotion]] — `status: in-progress → in-review`.

### 2026-04-19 — slice-vault-sync first cut ready for review

Implements the bidirectional synchronization between the Obsidian vault and Qdrant. Ships a dedicated `musubi-vault-watcher` process using `watchdog` and `ruamel.yaml` for formatting-preserving frontmatter round-trips. Includes a sqlite-backed write log for echo prevention and a periodic drift reconciler.

Vault changes:
- [[_slices/slice-vault-sync]] — `status: in-progress → in-review`.
- [[06-ingestion/vault-sync]] — `status: draft → complete`.
- [[06-ingestion/vault-frontmatter-schema]] — updated `implements:`.

### 2026-04-19 — slice-retrieval-rerank first cut ready for review

Implements the cross-encoder reranking stage for the retrieval deep path. Uses `BAAI/bge-reranker-v2-m3` via a dedicated TEI instance. Rerank scores are normalized via sigmoid and replace the RRF-relevance component in the final composite score, providing a significant quality lift for ambiguous queries.

Vault changes:
- [[_slices/slice-retrieval-rerank]] — `status: in-progress → in-review`.

### 2026-04-18 — eric — cleanup: retired `feat/lifecycle-scripts` (v1 orphan)

Deleted stale v1-era branch `origin/feat/lifecycle-scripts` (4 commits dated 2026-04-06, all touching the v1 `musubi/` package that v2 replaced). No changes from those commits transfer cleanly to v2's architecture; branch was obstructing the GitHub UI's PR-suggestion prompts.

Commits preserved in git graph (reachable by SHA on GitHub for months):

- `f8ed1fb` — Add lifecycle scripts: install, update, uninstall *(already squash-merged to main as PR #2 → `a9dadc4`)*
- `e3d92be` — Slim response payloads and add session_sync for context preservation
- `011b50a` — Enhance development setup and memory handling
- `501e115` — Fix critical thought/memory bugs and harden architecture

The `session_sync` concept (from `e3d92be`) is the only idea worth revisiting — if v2 ever wants persistent-session context beyond what the plane model provides, that's the historical reference point. Recover via `git fetch origin 501e115:recovered/v1-lifecycle-scripts`.

Vault changes:
- (this entry, no spec-level changes)

### 2026-04-18 — Inference weights pre-staged; Open WebUI reachable to Ollama

Two things landed on the Musubi host ahead of the compose stack.

**Weights cached locally** so the eventual Ansible-managed compose bring-up doesn't have to re-download ~12 GB on first boot:

- **Ollama:** `qwen2.5:7b-instruct-q4_K_M` pulled (4.68 GB). All earlier Ollama models (`pony-prompter`, `dolphin3:8b`, `llama3.1:8b`, `nomic-embed-text`) removed — `qwen2.5` is the only one Musubi's spec calls for (see [[08-deployment/compose-stack]]).
- **HuggingFace cache** at `~<operator>/musubi-hf-cache/hub/` on the Musubi host (6.9 GB total) — `BAAI/bge-m3` (4.3 GB, dense), `naver/splade-v3` (419 MB, sparse, gated repo — needed HF token), `BAAI/bge-reranker-v2-m3` (2.2 GB, reranker). When the `tei-models` Docker named volume gets created by compose, this directory either gets `rsync -a`'d into the volume or the compose file is changed to bind-mount the host path.

**Open WebUI fixed** — chat against Qwen was hanging. Two config bugs:

1. `OLLAMA_BASE_URL` was `http://127.0.0.1:11434` but Ollama's systemd env sets `OLLAMA_HOST=<musubi-ip>:11434` (VLAN-only, no loopback). Fixed to the VLAN IP.
2. `CORS_ALLOW_ORIGIN` used commas; Open WebUI's parser splits on `;` only (`os.environ.get('CORS_ALLOW_ORIGIN', '*').split(';')` in `open_webui/config.py:1639`). The whole comma-joined string was treated as one origin, socket.io rejected every WebSocket handshake, UI hung on the logo. Fixed to semicolon delimiter.

Both gotchas captured in [[08-deployment/host-profile#Known deployment gotchas]] so the future `roles/open-webui/` (or whoever ends up owning Open WebUI under compose) doesn't re-hit them.

Vault changes:

- [[00-index/work-log]] — this entry.
- [[08-deployment/host-profile]] — "Actual deployed state" section: Ollama bullet lists which model is pulled; Open WebUI bullet notes config fixed; new "Inference model cache" bullet documents the HF cache location + contents; new "Known deployment gotchas" subsection documenting the two Open WebUI fixes.
- No slice status changes — slice-types and slice-qdrant-layout remain `in-progress`; slice-ops-ansible and slice-ops-compose remain `ready` (this work is prep, not a slice flip).

### 2026-04-18 — Kong replaces Caddy as the API gateway (ADR-0014)

The spec's Caddy-on-the-Musubi-host design was superseded by routing Musubi's external API through the existing VLAN-wide Kong gateway on `<kong-gateway>` (`<kong-ip>`). Rationale: single gateway for the fleet, richer plugin ecosystem, no parallel cert story, simpler Musubi host.

Musubi host now exposes exactly one port to the LAN: `<musubi-ip>:8100` (plain HTTP, Kong's only upstream). Inference stack (Qdrant, TEI, Ollama) stays bridge-only. TLS terminates at Kong.

Vault changes (swept):

- [[13-decisions/0014-kong-over-caddy]] — new ADR.
- `08-deployment/caddy.md` **removed**; replaced by [[08-deployment/kong]] (new, comprehensive route + plugin doc).
- [[08-deployment/host-profile]] — ports table rewritten (no `:443`, `:8100` now bound to the Musubi host); systemd units trimmed; firewall rules updated to allow `:8100` from Kong's IP only.
- [[08-deployment/compose-stack]] — "no gateway on this host" language; Kong's upstream reach documented.
- [[08-deployment/ansible-layout]] — `roles/caddy/` removed; replaced with a "Gateway (lives elsewhere)" section documenting Musubi's expectations of Kong without owning Kong's config.
- [[08-deployment/index]] — topology diagram shows Kong on `<kong-gateway>` fronting Musubi; bring-up order notes Kong is independent.
- [[03-system-design/components]], [[03-system-design/process-topology]], [[03-system-design/failure-modes]] — gateway references renamed.
- [[07-interfaces/canonical-api]], [[07-interfaces/mcp-adapter]] — base-URL / auth-flow language updated.
- [[10-security/index]], [[10-security/auth]], [[10-security/data-handling]], [[10-security/audit]] — TLS / rate-limit / audit-log attribution moved from Caddy to Kong.
- [[09-operations/runbooks]], [[09-operations/capacity]], [[09-operations/observability]] — gateway ops guidance re-pointed at Kong.
- [[11-migration/scaling]], [[11-migration/phase-7-adapters]], [[11-migration/index]] — migration notes updated.
- [[_slices/slice-ops-ansible]], [[_slices/slice-ops-compose]] — slice descriptions dropped Caddy.
- [[02-current-state/gap-analysis]] — TLS row now names Kong.
- [[08-deployment/qdrant-config]] — defense-in-depth paragraph still applies; gateway name updated.

Historical references preserved in [[13-decisions/0010-single-host-v1]] (discussed Caddy as load-balancer option) and [[13-decisions/sources]] (Caddy the tool listed among references). Not swept.

### 2026-04-17 — slice-qdrant-layout first cut on `v2`

`src/musubi/store/` is on `v2` as commit `0f46281`: collection specs + index registry + idempotent bring-up (`ensure_collections`, `ensure_indexes`, `bootstrap`). All 7 collections per [[04-data-model/qdrant-layout#Collections]] are declared — BGE-M3 1024-d cosine + INT8 scalar quantization + HNSW m=32/ef_construct=256 dense, SPLADE++ V3 sparse on 5/7. Universal payload indexes + per-collection deltas cover the full table from the spec.

Idempotency works in both deployment modes: real server reports `payload_schema`, local/in-memory mode no-ops silently. Mock-based tests verify call shape + skip-on-existing; integration tests against a live Qdrant are deferred to `tests/integration/`.

`qdrant-client>=1.12` is now a runtime dep (1.17.1 installed). `make check` clean: ruff format + lint + `mypy --strict` on 36 files + 156 pytest (46 new for this slice on top of slice-types' 110).

Vault changes:

- [[_slices/slice-qdrant-layout]] — `status: ready → in-progress`; `owner: unassigned → eric`; `owns_paths` updated from the pre-monorepo `musubi/collections.py` + `musubi/qdrant_bootstrap.py` to `src/musubi/store/` (matches ADR 0015 + the spec's own Test Contract naming); work-log entry with the full diff summary.
- `slice-plane-episodic`, `slice-plane-curated`, `slice-plane-artifact`, `slice-plane-concept`, `slice-retrieval-hybrid` remain `ready` — the structural prereq is now in flight.

### 2026-04-17 — slice-types first cut on `v2`

Shared pydantic foundation landed as commit `9d57c37` on the `v2` branch. `src/musubi/types/` now holds `MusubiObject` + `MemoryObject` bases (bitemporal validity, lineage fields, monotonicity invariants), all five concrete memory types (`EpisodicMemory`, `CuratedKnowledge`, `SynthesizedConcept`, `Thought`, `SourceArtifact` + `ArtifactChunk`), `ArtifactRef`, `LifecycleEvent` with the transition table from [[04-data-model/lifecycle#Allowed transitions per type]], and `Result[T, E]` via PEP 695 type params.

KSUID dep swapped from the PyPI `ksuid` (40-char hex) to `svix-ksuid` (27-char base62) — the form the vault actually mandates. Validators cover namespace regex (`tenant/presence/plane`), UTC-only datetimes, `valid_from <= valid_until`, self-supersession, and sha256 hex.

`make check` clean: ruff format + lint + `mypy --strict` on 23 source files + 110/110 pytest. Qdrant-payload roundtrip is deferred to `slice-qdrant-layout`.

Vault changes:

- [[_slices/slice-types]] — `status: ready → in-progress`; `owner: unassigned → eric`; work-log entry with the full diff summary; ready to flip to `done` after downstream slices confirm the shape is sufficient.
- No other slice state changed; blocked slices (`slice-plane-episodic`, `slice-lifecycle-engine`, `slice-api-v0`, …) remain `ready` for pickup.

### 2026-04-17 — Musubi v2 scaffold pushed; monorepo decision (ADR-0015)

v2 rebuild started. The `v2` branch of `github.com/ericmey/musubi` now holds a clean Python 3.12 + `uv` + pydantic v2 scaffold (hatchling build, ruff + mypy strict + pytest, GitHub Actions CI), committed as `6457881` and pushed over SSH (HTTPS credential helper wasn't wired; switched remote to `git@github.com`). v1 content was removed from the `v2` branch entirely — `main` still holds v1 as the historical POC; v2 is the new source of truth and will merge to `main` at feature parity.

Alongside the scaffold, the repo-layout portion of [[13-decisions/0011-canonical-api-and-adapters]] was superseded: Musubi is a **monorepo**, not eight repos. Interface discipline (canonical API, adapters only talk via the SDK, no storage reach-throughs) carries over as import-lint rules instead of repo fences. All components — Core, SDK, MCP/Obsidian/CLI adapters, contract tests, `deploy/` (Ansible + Compose) — live under `src/musubi/` in the one repo.

Vault changes:

- [[13-decisions/0015-monorepo-supersedes-multi-repo]] — new ADR; supersedes 0011's repo-layout portion.
- [[13-decisions/0011-canonical-api-and-adapters]] — status flipped to `partially-superseded`; callout banner pointing at 0015; interface-discipline half of the ADR still stands.
- [[13-decisions/index]] — static index now lists 0013, 0014, 0015; 0011 annotated as partially superseded.
- [[12-roadmap/ownership-matrix]] — "Repos" table collapsed from 8 rows to 1 (monorepo + vault as separate human-authored repo); module table updated to the `src/musubi/` layout including `sdk/`, `adapters/{mcp,obsidian,cli}/`, `contract_tests/`; import-discipline lint rules documented.
- No slice flips: `slice-types` remains `ready` (not `in-progress`) until first code lands on `v2` under `src/musubi/types/`.

### 2026-04-17 — Proxmox hosts joined the Ansible fleet

`<pve-node-1>` (`<pve-ip-1>`) and `<pve-node-2>` (`<pve-ip-2>`) now accept the operator + Ansible keys on `root@pam` via SSH. `ansible proxmox_nodes -m ping` is green from `<ansible-control>`; Semaphore's `Ping all` template hits 6-for-6 across the homelab fleet (two Proxmox nodes, Ansible control, Kong gateway, `<immich-host>`, Musubi host). These two hosts were the last unmanaged members of the homelab fleet.

This doesn't directly realise a Musubi spec — the Proxmox hosts aren't part of Musubi's deployment surface — but it unblocks future Ansible-driven host patching, which the spec in [[08-deployment/ansible-layout]] assumes is possible.

Vault changes:

- (this entry, no spec-level changes)

### 2026-04-17 — Musubi host provisioned

Physical machine online as `<musubi-host>` (`<musubi-ip>`). Ryzen 5 5500 + RTX
3080 10 GB + 15 GB RAM + 1.8 TB NVMe on Ubuntu 24.04.4. Qdrant (`:6333`/`:6334`)
and Ollama (`:11434`) running natively (pre-Ansible manual install). Added to
external homelab Ansible inventory (`musubi_hosts` group). See
[[08-deployment/host-profile#Actual deployed state]] for the gap list from
spec → reality. Slice registry in [[_slices/index]] is untouched — Musubi Core
itself is not yet built.

Vault changes:

- [[08-deployment/host-profile]] — added `deployment_status: provisioned`, `provisioned_at: 2026-04-17` frontmatter; added **Actual deployed state** section with hardware delta table and explicit gap checklist.

## How to add an entry

```markdown
### YYYY-MM-DD — <one-line headline>

One or two paragraphs on what happened and why it matters.

Vault changes (which notes did you edit, and with what field flip):
- [[path/to/note]] — what changed
- [[_slices/slice-xyz]] — `status: ready → in-progress`
```

Keep it tight. Headlines searchable. Newest on top. Don't rewrite old entries;
add new ones if context evolves.

## Frontmatter fields for realization tracking

When a spec describes infrastructure that has come online, add these fields to
its frontmatter (in addition to `status:`):

| Field | When to use |
|---------------------|--------------------------------------------------------------------------|
| `deployment_status` | `planned` → `provisioned` → `operational` → `retired` |
| `provisioned_at` | Date the physical/virtual resource came online. |
| `operational_at` | Date the service began serving production traffic (after config + test). |
| `retired_at` | Date the resource was decommissioned. |

These are optional — use on notes where the spec vs. reality distinction
matters (host profile, adapter deployments, etc.). Don't bother on purely
abstract design notes.

## Related

- [[00-index/dashboard]] — live status snapshot.
- [[_slices/index]] — machine-readable slice state.
- [[_slices/completed-work]] — slices marked `status: done`.
