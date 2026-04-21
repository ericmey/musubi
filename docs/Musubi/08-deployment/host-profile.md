---
title: Host Profile
section: 08-deployment
tags: [cuda, deployment, host, section/deployment, status/complete, type/spec, ubuntu, deployed]
type: spec
status: complete
deployment_status: provisioned
provisioned_at: 2026-04-17
updated: 2026-04-20
up: "[[08-deployment/index]]"
reviewed: true
implements: "docs/Musubi/08-deployment/"
---
# Host Profile

The dedicated Ubuntu box. What it looks like, how it's provisioned, how components fit into it.

> Concrete hostnames and IPs in this spec use placeholder tokens (`<musubi-host>`, `<musubi-ip>`, `<kong-gateway>`, `<homelab-domain>`, etc.). Real values live in `.agent-context.local.md` at the repo root (gitignored); agents substitute when running real commands.

> **Deployed 2026-04-17.** A physical machine matching this profile is online as `<musubi-host>` on the homelab VLAN. Base services (Qdrant, Ollama) are running natively (not containerised) from a pre-Ansible manual install. The concrete realised state (exact hardware serials, packages pulled, services and ports in use right now) is in `.agent-context.local.md` § *Realised deployment state (2026-04-18)*. See [[00-index/work-log]] for the dated event.

## Hardware

| Component | Spec                                                    | Deployed (summary)                                       |
|-----------|---------------------------------------------------------|----------------------------------------------------------|
| CPU       | AMD Ryzen 5 (6c/12t)                                    | AMD Ryzen 5 Zen 3 (6c/12t) ✓                              |
| RAM       | 32 GB DDR4                                              | 15 GB ⚠ — below spec; add 16 GB before v1 load tests      |
| GPU       | NVIDIA RTX 3080, 10 GB VRAM                             | NVIDIA GeForce RTX 3080, 10 GB ✓                          |
| Storage   | 1 TB NVMe (primary); 4 TB SATA SSD (snapshots)          | 1.8 TB NVMe ✓ primary; SATA SSD not yet present           |
| Network   | 1 GbE                                                   | 1 GbE ✓ on `<musubi-vlan>`, static `<musubi-ip>`          |
| Role      | Dedicated; no shared workloads                          | Dedicated ✓                                              |

The 10 GB VRAM is the constraint that shapes everything in [[08-deployment/gpu-inference-topology]].

## OS

- Ubuntu Server 24.04 LTS, minimal install.
- Unattended security upgrades via `unattended-upgrades`.
- Swap: 16 GB on NVMe (emergencies only; Musubi avoids swapping).
- Time: `chrony` synced to `time.cloudflare.com`.

## Filesystem layout

```
/etc/musubi/                # config, compose files
/var/lib/musubi/
  qdrant/                   # Qdrant storage
  vault/                    # vault (bind-mount into Core container)
  artifact-blobs/           # content-addressed artifact blobs
  lifecycle-work.sqlite     # write-log, schedule locks, cursors
/var/log/musubi/            # Core logs rotated daily
/opt/musubi/                # source clones + binaries
/mnt/snapshots/             # snapshot target (SATA SSD)
```

`/var/lib/musubi/vault/` is the Obsidian vault. Also mounted by user on their laptop via Syncthing — but see [[09-operations/backup-restore]] for the full sync story.

## CUDA + drivers

- NVIDIA driver: `nvidia-driver-560` (stable for CUDA 13 as of April 2026).
- CUDA: installed inside containers via base images (no host-side CUDA install).
- Tested with `nvidia-container-toolkit` for Docker GPU passthrough.

Verify:

```
nvidia-smi                            # host sees the 3080
docker run --rm --gpus all ubuntu:24.04 nvidia-smi   # container sees it
```

## Docker

- Docker Engine (apt repo), plus `docker-compose-plugin`.
- Rootful daemon (Musubi services need GPU access; rootless GPU is fragile in April 2026).
- Compose file: `/etc/musubi/docker-compose.yml` — managed by Ansible.

## Systemd units

- `musubi.service` — wraps `docker compose up` with `Restart=always`. Only unit needed; no separate gateway service (Kong runs on `<kong-gateway>`, not here — see [[13-decisions/0014-kong-over-caddy]]).

```ini
# /etc/systemd/system/musubi.service
[Unit]
Description=Musubi stack
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=/etc/musubi
ExecStart=/usr/bin/docker compose up
ExecStop=/usr/bin/docker compose down
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Ports

| Port | Bound to | Who |
|---|---|---|
| 8100 | `<musubi-ip>` (VLAN) | Musubi Core — plain HTTP, Kong's only upstream |
| 6333 | bridge only | Qdrant REST (compose network) |
| 6334 | bridge only | Qdrant gRPC (compose network) |
| 8010 | bridge only | TEI dense |
| 8011 | bridge only | TEI sparse |
| 8012 | bridge only | TEI reranker |
| 11434 | bridge only | Ollama |

The Musubi host itself does **not** terminate TLS and does **not** expose a :443 port. TLS terminates at **Kong on `<kong-gateway>` (`<kong-ip>`)**; Kong's only upstream for Musubi is `http://<musubi-ip>:8100`. See [[08-deployment/kong]] for the gateway config.

All inference services run on Docker's compose bridge — not exposed on the host network. Inter-service traffic uses compose DNS (`qdrant:6333`, `tei-dense:80`, etc.). Admin access to these services is via `ssh <musubi-host>` + `docker exec`, not host ports.

## User / process model

- `musubi` system user owns `/var/lib/musubi/*` and runs the Docker stack (in the `docker` group).
- Core runs as uid `10001` inside the container (non-root).
- Vault bind-mount uses `musubi:musubi` ownership on the host.

## Logging

- Each container logs to journald (`docker logging driver=journald`).
- Core additionally writes structured JSON to `/var/log/musubi/core.log`, rotated daily (`logrotate.d`).
- 30-day retention by default; configurable.

## Firewall

- `ufw` enabled. Ingress policy:
  - **22 (SSH)** — admin subnet only.
  - **8100 (Musubi Core)** — only `<kong-gateway>` (`<kong-ip>/32`), Kong's upstream connection.
  - Nothing else accepted from the LAN.
- SSH key-only; password auth disabled.
- Optional: Tailscale if access from outside LAN is needed. Runs as a separate unit; `/etc/musubi` unrelated.

## Provisioning

Everything above is captured in [[08-deployment/ansible-layout]]. A fresh host reaches production-equivalent state in one playbook run.

## Capacity headroom

At typical load (a few thousand captures/day, a couple hundred retrievals/hour):

- CPU: < 20% avg, spikes during synthesis batches.
- RAM: ~14 GB (Qdrant working set + Python + model tokenizers).
- VRAM: ~9 GB hot, 1 GB reserve. See [[08-deployment/gpu-inference-topology]] for the schedule.
- NVMe: ~5 GB / month growth (vault + Qdrant); multi-year runway.

Monitored continuously; alerts at 75% on any dimension. See [[09-operations/alerts]].

## Failure modes

- **GPU OOM** → Ollama or a TEI service gets killed. Compose restarts. Fast-path briefly degrades to cache-only.
- **Qdrant crash** → Compose restarts; ~30s warm-up. Any pending captures retry (idempotency + SDK retry).
- **NVMe fill** → Core enters read-only mode at 90% full, logs alert.
- **Power loss** → Qdrant WAL + SQLite WAL ensure clean recovery; at most one in-flight mutation lost (caller retries).

## Non-goals for v1

- Hot standby / HA. See [[11-migration/scaling#high-availability]] for the plan.
- Autoscaling. One box is the capacity plan.
- Multi-tenant isolation at the host level. Token scope handles tenant separation logically.

## Actual deployed state

The point-in-time snapshot of what's actually running on the reference host — services, ports, model weights cached, config knobs applied — is maintained in `.agent-context.local.md` at the repo root under § *Realised deployment state*. That file is gitignored because it names concrete internal IPs / hostnames.

**Public-safe summary (as of 2026-04-20, first real deploy):** the full Musubi
compose stack is running. Six services healthy:

| Container              | Image                                              | Role                       |
|------------------------|----------------------------------------------------|----------------------------|
| `musubi-core-1`        | `musubi-core:dev` (locally built — see repo `Dockerfile`) | FastAPI API + lifecycle orchestration |
| `musubi-qdrant-1`      | `qdrant/qdrant:v1.17.1`                            | Vector DB, api-key-auth on |
| `musubi-tei-dense-1`   | `ghcr.io/huggingface/text-embeddings-inference:86-1.2.0` | BGE-M3 dense embeddings |
| `musubi-tei-sparse-1`  | same                                               | SPLADE v3 sparse embeddings |
| `musubi-tei-reranker-1`| same                                               | BGE-reranker-v2-m3 cross-encoder |
| `musubi-ollama-1`      | `ollama/ollama:latest`                             | Qwen 3 4B LLM              |

Health: `curl http://<musubi-ip>:8100/v1/ops/health → {"status":"ok","version":"v0"}`.
GPU usage at rest: ~3.3 GiB of 10 GiB VRAM.

The native Qdrant / Ollama / Open WebUI installs that occupied the host before
the first compose deploy were stopped, disabled, and purged (binaries,
`/etc/qdrant/`, `/var/lib/{qdrant,open-webui}`, `/usr/share/ollama/`, service
users). The `bootstrap.yml` playbook assumes a greenfield host; pre-existing
services are a one-time artefact of this specific migration and not a pattern
to codify.

The HF cache under `/home/ericmey/musubi-hf-cache/hub/` (BGE-M3, SPLADE v3,
BGE-reranker-v2-m3) was rsynced into `/var/lib/musubi/tei-models/` before the
compose stack came up. That preserved ~6.9 GB of downloads (SPLADE v3 is
gated on HuggingFace and would 401 otherwise). Automating this rsync in
`bootstrap.yml` is a tracked follow-up (see [[00-index/work-log]] 2026-04-20).

## Known deployment gotchas

Captured from operational work on 2026-04-18. The future Ansible role that eventually templates Open WebUI (and/or the compose config that replaces it) must honour both:

1. **Ollama binds to its `OLLAMA_HOST` value, not loopback.** If the systemd unit sets `OLLAMA_HOST=<musubi-ip>:11434`, there is **no listener on `127.0.0.1:11434`**. Any client (Open WebUI, `ollama list` from an interactive shell, future Musubi Core, etc.) that defaults to `127.0.0.1:11434` will fail with "could not connect." Set `OLLAMA_BASE_URL` (Open WebUI) / `OLLAMA_URL` (Musubi Core) to the explicit VLAN endpoint or (in compose) to the service-name DNS (`http://ollama:11434`).

2. **Open WebUI's `CORS_ALLOW_ORIGIN` splits on `;`, not `,`.** Source: `open_webui/config.py:1639` — `os.environ.get('CORS_ALLOW_ORIGIN', '*').split(';')`. A comma-separated list is treated as a *single* invalid origin; the socket.io layer then rejects every WebSocket handshake with `"<origin> is not an accepted origin"` and the UI hangs on the logo screen. Multi-origin lists **must** use semicolons:
   ```
   CORS_ALLOW_ORIGIN=http://<musubi-ip>:8080;http://<musubi-host>:8080;https://<musubi-host>:8080
   ```

## Gap list before this spec is "realized"

- [ ] Add 16 GB RAM (currently 15 GB of 32 GB target; compose stack runs
      comfortably within this for now but the 32 GB spec target stands).
- [ ] Add 4 TB SATA SSD for snapshots + artifact-blob mount.
- [ ] Replace ad-hoc Samba share with Syncthing for the vault.
- [x] Lift native Qdrant / Ollama installs into the Ansible-managed Docker
      Compose layout (per [[08-deployment/compose-stack]]) — done 2026-04-20.
      Native services purged; compose stack is the only inference path.
- [x] Create Musubi system user `musubi` with the ownership model described
      above — done 2026-04-20 via `bootstrap.yml` (uid 999, gid 985).
- [ ] Decide on Kong route(s) under `<homelab-domain>` (internal) or
      `<external-domain>` (external) for Musubi Core. **Deferred** per
      [[13-decisions/0024-kong-deferred-for-musubi-v1]]; Musubi is
      VLAN-internal only today.
- [x] Wire up the TEI model cache — done 2026-04-20. One-time rsync from
      `~ericmey/musubi-hf-cache/hub/` into `/var/lib/musubi/tei-models/`.
      Automating this in `bootstrap.yml` is a follow-up.
- [x] Pull the LLM and embedding model weights used by Musubi — done
      2026-04-18 for BGE-M3 / SPLADE v3 / BGE-reranker-v2-m3; done 2026-04-20
      for Qwen 3 4B (auto-pulled by `deploy.yml`'s `ollama pull` step).
      The pre-staged Qwen 2.5 7B was discarded when the native Ollama was
      purged — the spec and deploy now agree on Qwen 3 4B per
      [[13-decisions/0019-qwen-on-musubi-gpu-phase-1]].

Each of these is a candidate for future `_slices/slice-ops-*` or `slice-musubi-*` work.

## Test Contract

Realized by **[[_slices/slice-ops-ansible]]** (status: done) — see that slice's `## Test Contract` section for the canonical bullet list and the test-file pointers that verify each bullet.
