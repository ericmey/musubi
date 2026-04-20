---
title: Work Log
section: 00-index
type: index
status: living-document
tags: [section/index, status/living-document, type/index]
updated: 2026-04-18
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

| Field               | When to use                                                              |
|---------------------|--------------------------------------------------------------------------|
| `deployment_status` | `planned` → `provisioned` → `operational` → `retired`                     |
| `provisioned_at`    | Date the physical/virtual resource came online.                           |
| `operational_at`    | Date the service began serving production traffic (after config + test).  |
| `retired_at`        | Date the resource was decommissioned.                                     |

These are optional — use on notes where the spec vs. reality distinction
matters (host profile, adapter deployments, etc.). Don't bother on purely
abstract design notes.

## Related

- [[00-index/dashboard]] — live status snapshot.
- [[_slices/index]] — machine-readable slice state.
- [[_slices/completed-work]] — slices marked `status: done`.
