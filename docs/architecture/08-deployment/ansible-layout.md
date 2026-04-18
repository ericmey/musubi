---
title: Ansible Layout
section: 08-deployment
tags: [ansible, deployment, provisioning, section/deployment, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-18
up: "[[08-deployment/index]]"
reviewed: false
---
# Ansible Layout

Ansible is the bring-up tool. One playbook provisions a fresh Ubuntu box to a Musubi-ready state. Re-running it is safe (idempotent).

**Repo:** `musubi-infra` (separate from Core). Kept deliberately thin — ~500 lines of YAML.

> Hostnames and IPs use placeholder tokens. Real values in `.agent-context.local.md` (gitignored).

## Why Ansible

- Works against any Ubuntu box over SSH. No agent to install first.
- Idempotent by construction — re-run any time.
- Single source of truth for everything that lives on the host (beyond containers): users, filesystems, drivers, systemd units.
- Integrates with local 1Password or a `.vault.yml` for secrets (GEMINI keys, token signing keys, etc.).

We don't use Terraform, Kubernetes, or Helm. The host is fixed; the containers are managed by Compose. Ansible glues the two.

## Structure

```
musubi-infra/
  inventory/
    hosts.yml                  # host list (one entry for v1)
    group_vars/
      all.yml                  # defaults
      musubi.yml               # musubi-specific overrides
  roles/
    base/                      # user, timezone, apt upgrade, ufw, logrotate
    nvidia/                    # driver install + nvidia-container-toolkit
    docker/                    # Docker CE + compose plugin
    musubi-fs/                 # /var/lib/musubi layout + permissions
    musubi-stack/              # compose.yml render + systemd unit
    backup/                    # cron entries for snapshot + git push
  playbooks/
    musubi.yml                 # full bring-up
    update.yml                 # pulls compose image digests, restarts
    rotate.yml                 # log + snapshot retention
  .vault.yml                   # encrypted secrets (git-crypt or ansible-vault)
  requirements.yml             # collection deps (community.docker, etc.)
```

## Inventory

```yaml
# inventory/hosts.yml
all:
  children:
    musubi:
      hosts:
        musubi-1:
          ansible_host: 192.168.1.42
          ansible_user: eric
          ansible_become: true
          musubi_hostname: musubi.internal.example.com
          musubi_cert_mode: selfsigned   # or 'letsencrypt'
```

Variables per host keep it clear: change the host, re-run playbook. Secrets live outside this file.

## Roles

### `base`

```yaml
# roles/base/tasks/main.yml
- name: Create musubi user
  ansible.builtin.user:
    name: musubi
    groups: docker
    system: yes
    shell: /sbin/nologin

- name: Install unattended-upgrades
  ansible.builtin.apt:
    name: unattended-upgrades
    state: present

- name: Configure ufw
  community.general.ufw:
    rule: "{{ item.rule }}"
    port: "{{ item.port }}"
  loop:
    - {rule: allow, port: 22}
    - {rule: allow, port: 443}

- name: Enable ufw
  community.general.ufw:
    state: enabled
```

### `nvidia`

Installs `nvidia-driver-560` via apt + `nvidia-container-toolkit` so Docker can see the GPU.

```yaml
# roles/nvidia/tasks/main.yml
- name: Install NVIDIA driver
  ansible.builtin.apt:
    name: nvidia-driver-560-server
    state: present
  register: driver
  notify: reboot if driver changed

- name: Install nvidia-container-toolkit
  ansible.builtin.apt:
    name: nvidia-container-toolkit
    state: present

- name: Configure Docker to use NVIDIA runtime
  ansible.builtin.shell: nvidia-ctk runtime configure --runtime=docker
```

Driver changes trigger a reboot (handler), otherwise CUDA visibility is flaky.

### `docker`

- Adds Docker apt repo.
- Installs `docker-ce` + `docker-compose-plugin`.
- Enables `docker.service`.

### `musubi-fs`

- Creates `/var/lib/musubi/{qdrant,vault,artifact-blobs}`.
- Creates `/var/log/musubi/`.
- Chowns to `musubi:musubi`.
- Ensures `/mnt/snapshots` mount (optional external drive).

### `musubi-stack`

The core role:

1. Renders `/etc/musubi/docker-compose.yml` from a template.
2. Installs `/etc/systemd/system/musubi.service`.
3. Seeds `/etc/musubi/.env` from encrypted variables.
4. Optional: prefetches model images + weights (so first boot doesn't stall downloading multi-gigabyte models).

```yaml
# roles/musubi-stack/tasks/main.yml
- name: Render docker-compose.yml
  ansible.builtin.template:
    src: docker-compose.yml.j2
    dest: /etc/musubi/docker-compose.yml
    owner: root
    group: root
    mode: "0644"

- name: Seed .env
  ansible.builtin.template:
    src: env.j2
    dest: /etc/musubi/.env
    owner: root
    group: docker
    mode: "0640"
  no_log: true

- name: Pre-pull images
  community.docker.docker_image:
    name: "{{ item }}"
    source: pull
  loop: "{{ musubi_images }}"

- name: Install systemd unit
  ansible.builtin.template:
    src: musubi.service.j2
    dest: /etc/systemd/system/musubi.service
  notify: systemd daemon-reload

- name: Enable musubi service
  ansible.builtin.systemd:
    name: musubi
    enabled: yes
    state: started
```

### Gateway (lives elsewhere)

No gateway role in this repo. TLS termination, auth, rate-limiting, and access logging happen on **Kong, running on `<kong-gateway>` (`<kong-ip>`)** — outside Musubi's ownership. Kong's route config for Musubi lives in the Kong-admin repo, not here. See [[08-deployment/kong]] and [[13-decisions/0014-kong-over-caddy]].

What Musubi's Ansible *does* assume from the gateway:

- `<musubi-host>` resolves to Kong.
- Kong's upstream for this service is `http://<musubi-ip>:8100`.
- Kong's firewall allow-list includes this Musubi host (Kong is the only source IP the Musubi host's `ufw` accepts on `:8100`).

If those assumptions are unmet, Musubi doesn't fail to start — it just isn't reachable from clients until Kong is configured.

### `backup`

Cron entries:

- `0 */6 * * *` — Qdrant snapshot to `/mnt/snapshots/qdrant/<date>`.
- `*/15 * * * *` — git commit + push vault changes (via `git-sync.sh`).
- `15 3 * * *` — sqlite backup (write-log + schedule-locks).

Full detail: [[09-operations/backup-restore]].

## Playbooks

### `playbooks/musubi.yml`

```yaml
- name: Provision Musubi host
  hosts: musubi
  become: true
  roles:
    - base
    - nvidia
    - docker
    - musubi-fs
    - musubi-stack
    - backup
```

### `playbooks/update.yml`

Pulls new image digests (respecting `compose.override.yml` pins) and recreates changed services with zero-downtime intent (stops one, starts new, checks health, repeats):

```yaml
- name: Update Musubi stack
  hosts: musubi
  become: true
  tasks:
    - name: Pull new images
      community.docker.docker_compose_v2_pull:
        project_src: /etc/musubi

    - name: Recreate changed services
      community.docker.docker_compose_v2:
        project_src: /etc/musubi
        state: present
        recreate: smart    # only changed containers
```

### `playbooks/rotate.yml`

Retention sweeps: 30-day log prune, 90-day snapshot prune, 180-day lifecycle-event prune (if operator-approved).

## Secrets

`.vault.yml` holds:

- `MUSUBI_JWT_SIGNING_KEY`
- `GEMINI_API_KEY` (only if using Gemini in addition to local models — optional in v1)
- OAuth client secrets for the MCP adapter
- GitHub deploy key for vault git-sync

Encrypted via `ansible-vault encrypt`. Password lives in 1Password.

## Dry-run

```
ansible-playbook playbooks/musubi.yml --check --diff
```

`--check` flags any drift without applying changes. Run as a weekly cron to catch manual edits on the box.

## When to use Ansible vs. Compose

- **Anything host-level** (user, driver, apt, systemd, fs layout) → Ansible.
- **Anything container-level** (service config, env vars, volumes, health checks) → Compose.
- **Anything inference-model-level** (model weights, hot-swap models) → Compose + [[08-deployment/gpu-inference-topology]].

The line is strict. If something falls between (e.g., vault path) Ansible creates the dir, Compose mounts it.

## Test contract

**Module under test:** `musubi-infra/`

1. `test_playbook_syntax` — `ansible-playbook --syntax-check` passes.
2. `test_playbook_idempotent_on_clean_vm` — first run creates; second run reports zero changes.
3. `test_secrets_never_logged` — `no_log: true` on every task that templates `.env`.
4. `test_compose_file_renders_to_valid_yaml` — the Jinja → YAML pipeline is tested.
5. `test_systemd_unit_boots_stack_to_healthy` — integration: boot unit, curl `/v1/ops/health`, assert 200.
6. `test_update_playbook_respects_digest_pins` — `update.yml` does not move past pinned digests.
