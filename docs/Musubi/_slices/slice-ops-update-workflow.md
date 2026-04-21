---
title: "Slice: update.yml — in-place upgrade for a running Musubi"
slice_id: slice-ops-update-workflow
section: _slices
type: slice
status: ready
owner: unassigned
phase: "8 Ops"
tags: [section/slices, status/ready, type/slice, ops, upgrade, ansible]
updated: 2026-04-20
reviewed: false
depends-on: ["[[_slices/slice-ops-core-image-publish]]"]
blocks: []
---

# Slice: update.yml — in-place upgrade for a running Musubi

> Adds a dedicated Ansible playbook for upgrading a live Musubi host
> without a full re-bootstrap: pulls the newly-published image, recreates
> only the changed containers, runs post-update health probes, and writes
> a dated upgrade entry. Complements `deploy.yml` (which is
> first-deploy-only) and `bootstrap.yml` (which is host-level-only).

**Phase:** 8 Ops · **Status:** `ready` · **Owner:** `unassigned`

## Why this slice exists

The current `deploy/ansible/deploy.yml` pulls images with
`policy: missing` — it only fetches images that aren't already on the
host. Running it a second time against a running stack with a newer
image tag is a **no-op**: the old image is still present, nothing new
gets pulled, `docker compose up` sees no diff, no containers get
recreated. The host keeps running the old bits forever.

There's no other playbook to lean on. `bootstrap.yml` installs
host-level packages (Docker, NVIDIA toolkit, users, UFW) — useful on a
fresh host, overkill on every deploy, and doesn't touch container state.
`config.yml` re-renders `.env.production` — needed when env changes, but
doesn't restart containers unless the env actually differs. `health.yml`
is a probe, not a mutator.

So updating a running Musubi today means one of:

- Operator manually runs `docker compose pull && docker compose up -d --
  force-recreate core` on the host. No Ansible trail, no healthcheck
  gating, no audit.
- Operator runs `bootstrap.yml` + `deploy.yml` from scratch. Over-
  powered: re-installs packages it doesn't need to, risks more than is
  warranted for an image bump.

`update.yml` fills the gap that sits between them: a minimal, auditable
upgrade path with per-service recreation and explicit health gating.

## Specs to implement

- [[08-deployment/ansible-layout]] — the playbook roster that this slice
  extends (adds an `update.yml` row).
- [[09-operations/runbooks]] — gets a new `upgrade.md` as a companion
  to `first-deploy.md`.
- [[13-decisions/0014-kong-over-caddy]] §Consequences — the
  "blue/green" swap story that `update.yml` is a first step toward
  (eventually).

## Owned paths (you MAY write here)

- `deploy/ansible/update.yml` (new — the playbook).
- `deploy/runbooks/upgrade.md` (new — the operator-facing runbook).
- `tests/ops/test_update_playbook.py` (new — structural tests on the
  playbook + runbook).
- `deploy/ansible/README.md` (edit — add `update.yml` to the Layout
  table and the "Per-deploy workflow" section).
- `docs/Musubi/08-deployment/ansible-layout.md` (edit — describe
  `update.yml`'s role in the playbook roster).

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `deploy/ansible/bootstrap.yml`, `config.yml`, `deploy.yml` — leave
  alone. This slice adds a new playbook, it does NOT modify the
  existing three. If behavior shared across them needs to change, open
  a cross-slice ticket.
- `.github/workflows/publish-core-image.yml` — owned by
  `slice-ops-core-image-publish`. Reference its outputs (the GHCR
  digest); don't modify its shape.
- `src/musubi/`, `openapi.yaml`, `proto/` — Ops slice; no code.
- `docs/Musubi/13-decisions/` — no new ADRs unless a decision is
  forced during implementation.

## Depends on

- [[_slices/slice-ops-core-image-publish]] (ready — must land first so
  `update.yml` has a pinned GHCR source to pull from. Attempting to
  upgrade against a local `musubi-core:dev` tag is nonsense; there's
  no "newer" to pull).

## Unblocks

- **Routine image bumps become safe.** Bump the digest in `group_vars`
  + run `update.yml` → new image pulled, old container replaced, health
  verified. No manual `docker compose` on the host.
- **Rollback on bad deploys.** Revert the `group_vars` commit, run
  `update.yml` again — pulls the previous digest and recreates.
  Current state has no concept of "roll back to the previous image."
- **Faster iteration on Core.** Publish a new image on merge to `v2`,
  bump the group_vars, run `update.yml` — end-to-end Core upgrade in
  one command.

## What lands in this slice

### 1. `deploy/ansible/update.yml`

Skeleton (final shape subject to review; behaviour fixed):

```yaml
- name: Update a running Musubi stack
  hosts: musubi
  become: true
  tasks:
    - name: Re-render compose + env in case they changed
      ansible.builtin.import_tasks: tasks/render-templates.yml
      # Extract the template-render tasks from deploy.yml / config.yml
      # into a shared task file so both playbooks use them.

    - name: Pull any image references that changed
      community.docker.docker_compose_v2_pull:
        project_src: "{{ musubi_config_dir }}"
        policy: always        # <-- the key difference from deploy.yml.
                              # deploy.yml uses `missing` (first-deploy);
                              # update.yml uses `always` (force-pull).

    - name: Diff running containers vs desired
      ansible.builtin.command:
        cmd: docker compose -f {{ musubi_config_dir }}/docker-compose.yml config --format=json
      register: compose_config
      changed_when: false

    - name: Recreate only services whose image has changed
      # `docker_compose_v2` with `recreate: always` on a per-service
      # list — recreating every service every time is overkill and
      # causes unnecessary downtime on unaffected services.
      community.docker.docker_compose_v2:
        project_src: "{{ musubi_config_dir }}"
        state: present
        pull: never          # we pulled above; don't re-pull here
        recreate: always
        wait: true
        wait_timeout: 300
        services: "{{ changed_services | default(['core']) }}"
        # The default narrow recreate targets core since that's the
        # most common image bump. Operator can override via
        # `-e changed_services='[core,tei-dense]'` etc.

    - name: Probe core health post-update
      ansible.builtin.uri:
        url: "{{ musubi_health_urls.core }}"
        status_code: 200
      retries: 12
      delay: 5
      register: core_health
      until: core_health.status == 200
      changed_when: false

    - name: Append an upgrade-record entry
      ansible.builtin.lineinfile:
        path: /var/log/musubi/upgrade-history.jsonl
        create: true
        owner: musubi
        group: musubi
        mode: "0640"
        line: "{{ lookup('pipe', 'date -Iseconds') }} services={{ changed_services | default(['core']) }} image_core={{ musubi_core_image }}"
```

Key behaviours:

- **`policy: always` on the pull** — distinct from `deploy.yml`. Forces
  docker to pull the image referenced in `docker-compose.yml` even if
  a matching local tag exists. Essential when the digest has changed
  but the tag stayed the same (the GHCR publish workflow re-tags
  `:v2` as the floating branch pointer).
- **Per-service recreate via `changed_services`** — default to `[core]`
  because Core is the most frequently-updated piece. Operator can
  override to recreate more than one. No "recreate everything"
  default because churning Qdrant / Ollama / TEI unnecessarily costs
  model-load time.
- **No `bootstrap.yml` re-run** — update assumes the host is already
  bootstrapped. If it isn't, fail fast rather than silently re-running
  apt + user-creation tasks.
- **Append-only upgrade log** at `/var/log/musubi/upgrade-history.jsonl`
  so operators can `tail -f` during a drift investigation.

### 2. `deploy/runbooks/upgrade.md`

Operator procedure. Structure mirrors `first-deploy.md`:

1. **Pre-flight** — confirm the new image is published
   (https://github.com/ericmey/musubi/pkgs/container/musubi-core), or
   confirm a `qdrant`/`tei`/`ollama` version bump is in `group_vars`.
2. **Bump the pin** — commit `musubi_core_image` (or other image) to
   the new `@sha256:<digest>` in `group_vars/all.yml`; push; pull on
   yua. (A future PR could automate this via Dependabot/Renovate.)
3. **Dry-run** — `ansible-playbook update.yml --check --diff` from yua
   against musubi. Expected output: the compose-up task shows the
   image change + which containers will recreate.
4. **Apply** — `ansible-playbook update.yml` without `--check`.
5. **Verify** — `deploy/smoke/verify.sh` round-trip; `/var/log/musubi/
   upgrade-history.jsonl` has a new entry.
6. **Rollback** — revert the `group_vars` commit; push; pull on yua;
   re-run `update.yml`. Operator decision: a dedicated `--rollback`
   flag is out of scope for this slice (keep the revert-and-rerun
   pattern until a real rollback story is needed).

Every section has Command / Expected output / Destructive / Rollback,
matching the `first-deploy.md` structure the existing smoke tests
assert.

### 3. `tests/ops/test_update_playbook.py`

- The playbook parses (YAML validation).
- The playbook's pull step uses `policy: always` (the critical
  difference from `deploy.yml`).
- The compose-up step uses `recreate: always`.
- The playbook probes `/v1/ops/health` post-recreate.
- The playbook writes to `/var/log/musubi/upgrade-history.jsonl`.
- The upgrade runbook has all of: Pre-flight / Bump the pin / Dry-run
  / Apply / Verify / Rollback — per the same structural-test pattern
  that `tests/ops/test_first_deploy_smoke.py` uses against
  `first-deploy.md`.

### 4. `deploy/ansible/README.md` + `docs/Musubi/08-deployment/ansible-layout.md`

Both gain an `update.yml` row in the playbook roster. Both get a
"Per-deploy workflow" addendum noting that `update.yml` replaces the
old "run `deploy.yml` a second time with a newer tag" pattern (which
didn't work).

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] `ansible-playbook --syntax-check update.yml` passes.
- [ ] `--check --diff` against the live `musubi.example.local` (before the
      pin bump) shows zero changes (idempotent no-op on an up-to-date
      stack).
- [ ] After a scratch `musubi_core_image` bump, `--check --diff` shows
      exactly one image pull + one container recreate, no unrelated
      churn.
- [ ] Real apply against `musubi.example.local` recreates Core cleanly;
      `/v1/ops/health` returns 200 post-apply;
      `upgrade-history.jsonl` has a new entry.
- [ ] `deploy/runbooks/upgrade.md` reads top-to-bottom as an operator
      procedure; every step has Command / Expected output / Rollback.
- [ ] `tests/ops/test_update_playbook.py` passes; branch coverage
      ≥ 80 %.
- [ ] Work-log entry in `[[00-index/work-log]]` recording the first
      successful upgrade via `update.yml`.

**Explicitly NOT in scope:**

- Blue/green deploys. This is in-place recreation with a
  `wait: service_healthy` gate — not blue/green. Blue/green is a
  separate, larger slice when we have spare host capacity.
- Automated digest bumps (Dependabot/Renovate). Keep the pin bump a
  human-reviewed PR until we've operated `update.yml` for a few cycles.
- Rolling upgrades across multiple Musubi hosts. Single-host per ADR
  0010; revisit when that ADR changes.

## Test Contract

**Playbook structural:**

1. `test_update_playbook_parses`
2. `test_update_pull_policy_is_always`
3. `test_update_recreates_only_named_services`
4. `test_update_does_not_invoke_bootstrap_tasks`
5. `test_update_probes_core_health_post_apply`
6. `test_update_writes_upgrade_history`

**Runbook structural:**

7. `test_upgrade_runbook_has_six_sections`
8. `test_upgrade_runbook_every_step_has_rollback`
9. `test_upgrade_runbook_mentions_revert_and_rerun_rollback`

**Behavior (integration — runs on a scratch docker host):**

10. `test_update_noop_on_unchanged_stack`
11. `test_update_recreates_core_on_image_bump`
12. `test_update_leaves_unchanged_services_running`

## Cross-slice tickets opened by this slice

- _(none expected; if `deploy.yml` needs a refactor to share template-
  rendering logic, open one to `slice-ops-first-deploy` since it owns
  `deploy.yml`.)_

## Work log

_(empty — awaiting claim)_

## PR links

_(empty)_
