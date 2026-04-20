# First Deploy Runbook

This is the operator procedure for bringing up the first production Musubi
host. Hostnames and addresses are placeholders; resolve `<musubi-host>`,
`<musubi-ip>`, `<ansible-host>`, `<kong-gateway>`, and `<homelab-domain>` from
the local, gitignored operator context before running commands.

## 1. Pre-flight

**Command:**

```bash
ssh <ansible-host> 'cd ~/Projects/musubi && git switch v2 && git pull --ff-only'
ssh <musubi-host> 'id && docker --version || true'
ssh <musubi-host> 'ss -tlnp 2>/dev/null | grep -E ":(11434|6333|6334|8080|8100)" || true'
ssh <musubi-host> 'systemctl is-active qdrant ollama open-webui 2>&1 | sed "s/^/pre-existing service: /"'
```

**Expected output:** the control node is on `v2`; the target accepts SSH; Docker
may be absent before bootstrap. The last two commands enumerate any
pre-existing native services (Qdrant 6333/6334, Ollama 11434, Open WebUI 8080)
that Ansible will need to reconcile with. **If any of those services are
already active**, the deploy is a *migration* rather than a greenfield install
— stop and choose one of: (a) stop native services before step 3, (b) run
compose stack alongside on alternate ports, (c) defer migration until a
maintenance window. Each path has different data-handling obligations; pick
before step 2.

**Failure modes:** if SSH fails, fix operator access or host firewall before
continuing. If the repo is dirty on the control node, stop and inspect the diff.
If pre-existing services are found and no migration plan exists yet, pause the
deploy.

**Destructive:** no

## 2. Snapshot target

The mechanism depends on the host's storage layout. Check
`.agent-context.local.md` → *Realised deployment state* for the concrete
filesystem on the current target, then use the matching path.

**Command:** one of the three paths below — run only the one that matches the
target host's storage. Do not run more than one.

**Path A — ZFS host (when `<pool>` is set):**

```bash
ssh <musubi-host> 'sudo zfs snapshot <pool>/musubi@pre-first-deploy'
ssh <musubi-host> 'sudo zfs list -t snapshot | grep pre-first-deploy'
```

**Path B — LVM host with free VG space:**

```bash
ssh <musubi-host> 'sudo vgs --noheadings -o vg_free_count ubuntu-vg'
ssh <musubi-host> 'sudo lvcreate -L 20G -s -n ubuntu-lv-pre-first-deploy ubuntu-vg/ubuntu-lv'
ssh <musubi-host> 'sudo lvs | grep pre-first-deploy'
```

**Path C — no snapshot mechanism available (ext4 without free VG, or any host
where A and B do not apply):** take a file-level backup of the mutable state
directories so a post-failure restore is possible. Adjust paths to the
services already running on the host (check `ss -tlnp` output from pre-flight).

```bash
ssh <musubi-host> 'sudo mkdir -p /root/pre-first-deploy && \
  sudo tar -czf /root/pre-first-deploy/state-$(date +%Y%m%d-%H%M).tar.gz \
    /etc/qdrant /var/lib/qdrant \
    /var/lib/open-webui \
    /usr/share/ollama/.ollama \
    /etc/systemd/system/qdrant.service \
    /etc/systemd/system/ollama.service \
    /etc/systemd/system/open-webui.service 2>/dev/null || true'
ssh <musubi-host> 'sudo ls -lh /root/pre-first-deploy/'
```

**Expected output:** one snapshot or tarball timestamped with the pre-deploy
date is listed on the host (regardless of which path was taken).

**Failure modes:** Path A fails if `<pool>` is unset or the dataset is wrong —
fall to Path B. Path B fails when the VG has zero free extents — fall to
Path C. Path C tar can skip missing paths (`|| true`) and still exits non-zero
on write errors; if the tarball is zero bytes, the state dirs listed above are
wrong for this host — update the paths before retrying.

**Destructive:** yes

**Rollback:**

- Path A: `sudo zfs rollback <pool>/musubi@pre-first-deploy`
- Path B: `sudo lvconvert --merge ubuntu-vg/ubuntu-lv-pre-first-deploy` (then
  reboot — the merge completes on activation)
- Path C: stop any new services the deploy started, then
  `sudo tar -xzf /root/pre-first-deploy/state-<timestamp>.tar.gz -C /` to
  restore the captured directories, and `sudo systemctl daemon-reload` before
  restarting the original services.

## 3. Run ansible playbook

**Command:**

```bash
cd ~/Projects/musubi
ansible-galaxy collection install -r deploy/ansible/requirements.yml
ansible-playbook -i deploy/ansible/inventory.yml deploy/ansible/bootstrap.yml
ansible-playbook -i deploy/ansible/inventory.yml deploy/ansible/config.yml --ask-vault-pass
```

**Expected output:** Ansible ends with `failed=0`; `/var/lib/musubi`,
`/etc/musubi`, Docker, NVIDIA runtime, and vault-templated config exist on the
target.

**Failure modes:** package failures usually mean apt mirror or NVIDIA repo
trouble; fix and rerun. Vault failures mean the operator secret source is not
available on the control node.

**Destructive:** yes

**Rollback:** rerun after fixing idempotent failures, or roll back the host
snapshot from step 2 if host packages or filesystem state are suspect.

## 4. Bring up compose stack

**Command:**

```bash
ssh <musubi-host> 'cd /etc/musubi && sudo docker compose --env-file .env.production -f docker-compose.yml config --quiet'
ssh <musubi-host> 'cd /etc/musubi && sudo docker compose --env-file .env.production -f docker-compose.yml up -d --wait --timeout 300'
```

**Expected output:** Compose config validates; `qdrant`, all TEI services,
`ollama`, and `core` report healthy within five minutes on a warm model cache.

**Failure modes:** model cache misses can exceed the warm-cache budget; watch
`docker compose logs ollama tei-dense tei-sparse tei-reranker`. Config failures
usually point at missing vault-rendered env values.

**Destructive:** yes

**Rollback:** run `docker compose down`, restore the previous
`/etc/musubi/docker-compose.yml` and `.env.production`, then retry or roll back
the host snapshot.

## 5. Install systemd units

**Command:**

```bash
sudo install -m 0644 deploy/systemd/*.service /etc/systemd/system/
ssh <musubi-host> 'sudo systemctl daemon-reload && sudo systemctl enable --now musubi-api musubi-lifecycle-worker musubi-vault-sync'
ssh <musubi-host> 'systemctl --no-pager --failed'
```

**Expected output:** `systemctl --failed` lists no Musubi services; journal tags
`musubi-api`, `musubi-lifecycle-worker`, and `musubi-vault-sync` are visible.

**Failure modes:** if a unit fails immediately, inspect
`journalctl -u <unit> -n 100 --no-pager`. Most first-deploy failures are missing
environment files or an unstarted Docker service.

**Destructive:** yes

**Rollback:** `sudo systemctl disable --now musubi-api musubi-lifecycle-worker
musubi-vault-sync` and remove the unit files from `/etc/systemd/system/`.

## 6. Configure Kong

> **Applicability:** this step is OPTIONAL and depends on whether Musubi is
> being exposed through the Kong gateway. Check
> `.agent-context.local.md` → *Homelab topology* to see whether the current
> deploy target has a Kong-routed address. If Musubi is VLAN-internal only
> (reached directly at `<musubi-host>:8100` via internal DNS), skip steps 6
> and 7 and proceed to step 8 — validate them against
> [[13-decisions/0024-kong-deferred-for-musubi-v1]] for the latest deferral
> rationale.

**Command:** (Kong-fronted deployments only)

```bash
deck gateway validate deploy/kong/musubi-prod.yml
deck gateway sync deploy/kong/musubi-prod.yml
```

**Expected output:** decK validates the file and reports only intended creates
or updates for the Musubi `/v1` and `/mcp` routes. For internal-only deploys
the expected output is "skipped — no Kong route; Musubi served directly at
`<musubi-host>:8100`."

**Failure modes:** validation failures are YAML or plugin-shape problems. Sync
failures usually mean the Kong admin endpoint or credentials are wrong. If
Kong is not yet serving the Musubi domain (the DNS A record still points at
`<musubi-ip>` directly), keep the config staged and re-run this step later.

**Destructive:** yes

**Rollback:** `deck gateway diff` against the previous declarative Kong config,
then sync the previous known-good file. For deploys that skipped this step no
rollback is needed.

## 7. TLS certificate

> **Applicability:** same as step 6. Internal-only Musubi on a trusted VLAN
> may run plain HTTP at `<musubi-host>:8100` — TLS termination is a Kong
> concern. If Kong was skipped, skip this step too.

**Command:** (Kong-fronted deployments only)

```bash
deck gateway dump --select-tag musubi-prod
curl -I https://<musubi-host>/healthz
```

**Expected output:** Kong serves a valid certificate for `<musubi-host>` and the
health route reaches Musubi through the gateway. For internal-only deploys:
`curl -I http://<musubi-host>:8100/v1/ops/health` returns `200 OK`.

**Failure modes:** certificate issuance is operator-owned. If DNS-01 or the
internal CA is not ready, leave Kong route config staged but do not go live.

**Destructive:** no

## 8. Smoke verify

**Command:**

```bash
MUSUBI_BASE_URL=https://<musubi-host> \
MUSUBI_TOKEN=<operator-token> \
deploy/smoke/verify.sh
```

**Expected output:** every smoke script emits `[PASS]`; the aggregate script
exits `0`.

**Failure modes:** `[FAIL] component ...` means readiness is degraded. Capture,
thoughts, or metrics failures identify the surface to inspect next.

**Destructive:** yes

**Rollback:** stop traffic at Kong, keep the stack running for logs, and run the
specific failing `deploy/smoke/check_*.sh` script after each fix.

## 9. Rollback procedure

**Command:**

```bash
ssh <kong-gateway> 'deck gateway sync /etc/kong/previous-musubi.yml'
ssh <musubi-host> 'cd /etc/musubi && sudo docker compose down'
ssh <musubi-host> 'sudo zfs rollback <pool>/musubi@pre-first-deploy'
```

**Expected output:** Kong no longer routes to the new Musubi upstream; containers
are stopped; the target dataset returns to the pre-deploy snapshot.

**Failure modes:** if rollback fails, leave Kong disabled and restore from the
hypervisor snapshot or backup runbook before accepting traffic.

**Destructive:** yes

**Rollback:** this step is the emergency rollback path; after it succeeds,
start again from step 1 with the observed failure fixed.

## 10. Go-live checklist

**Command:**

```bash
curl -fsS https://<musubi-host>/v1/ops/status
curl -fsS https://<musubi-host>/v1/ops/metrics | head
```

**Expected output:** `/v1/ops/status` is `ok`; metrics render Prometheus text;
the dashboard for the production host is green.

**Failure modes:** do not announce go-live until DNS, OAuth client registration,
TLS, smoke verification, backup target reachability, and observability are all
green.

**Destructive:** no
