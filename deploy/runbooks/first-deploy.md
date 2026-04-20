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
```

**Expected output:** the control node is on `v2`; the target accepts SSH; Docker
may be absent before bootstrap.

**Failure modes:** if SSH fails, fix operator access or host firewall before
continuing. If the repo is dirty on the control node, stop and inspect the diff.

**Destructive:** no

## 2. Snapshot target

**Command:**

```bash
ssh <musubi-host> 'sudo zfs snapshot <pool>/musubi@pre-first-deploy'
ssh <musubi-host> 'sudo zfs list -t snapshot | grep pre-first-deploy'
```

**Expected output:** a `pre-first-deploy` snapshot is listed for the target
dataset.

**Failure modes:** if the host does not use ZFS, take the equivalent hypervisor
snapshot or disk image and record the snapshot identifier in the deploy notes.

**Destructive:** yes

**Rollback:** use `sudo zfs rollback <pool>/musubi@pre-first-deploy` or restore
the recorded hypervisor snapshot before retrying the deploy.

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

**Command:**

```bash
deck gateway validate deploy/kong/musubi-prod.yml
deck gateway sync deploy/kong/musubi-prod.yml
```

**Expected output:** decK validates the file and reports only intended creates
or updates for the Musubi `/v1` and `/mcp` routes.

**Failure modes:** validation failures are YAML or plugin-shape problems. Sync
failures usually mean the Kong admin endpoint or credentials are wrong.

**Destructive:** yes

**Rollback:** `deck gateway diff` against the previous declarative Kong config,
then sync the previous known-good file.

## 7. TLS certificate

**Command:**

```bash
deck gateway dump --select-tag musubi-prod
curl -I https://<musubi-host>/healthz
```

**Expected output:** Kong serves a valid certificate for `<musubi-host>` and the
health route reaches Musubi through the gateway.

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
