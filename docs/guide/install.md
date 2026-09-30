# Install

## Try it on one machine

You need Docker and nothing else. The quickstart runs the whole server on
CPU, with smaller models than production:

```bash
git clone https://github.com/sourceblender/musubi && cd musubi
docker compose -f quickstart/docker-compose.yml up -d --wait   # first boot downloads ~2.5 GB of models
docker compose -f quickstart/docker-compose.yml run --rm demo
```

The demo has one agent capture memories. A second agent, holding a read-only
token, then recalls the right one, and the demo checks that the read-only
token is refused a write. It exits non-zero if any step fails.

The API is then on `http://127.0.0.1:8100/v1`. The quickstart's signing key
is public (it's in `quickstart/.env.quickstart`), so never expose this stack
beyond your own machine.

Stop it with `docker compose -f quickstart/docker-compose.yml down`
(add `-v` to delete its data).

## Run it for real: one host

The supported production path is Ansible, driven from a separate control
machine. Start with the [first-deploy runbook](../../deploy/runbooks/first-deploy.md)
and [`deploy/ansible/`](../../deploy/ansible/README.md). The playbooks install
Docker and the NVIDIA container runtime on the host, render the Compose stacks
from [`deploy/ansible/templates/`](../../deploy/ansible/templates/) with the
pinned images in
[`group_vars/all.yml`](../../deploy/ansible/group_vars/all.yml), and run a
credential preflight against the new image before each deploy.

**What the host needs:**

- Linux with systemd. The stack logs to journald, and the runbook assumes
  Ubuntu.
- An NVIDIA GPU. The three text-embeddings-inference services and Ollama
  reserve it.
- Docker with Compose v2 (the playbooks install both).

**The root [`docker-compose.yml`](../../docker-compose.yml) is a scaffold,
not a runnable stack.** Every image line in it carries a placeholder digest.
Don't `up` it as committed; use the rendered stack.

### Pin and verify the image

Images are published to `ghcr.io/sourceblender/musubi-core`, signed with
cosign, scanned, and carry an SBOM. Tags move and digests don't, so pin the
digest from the release you want (its GitHub Release page, or
`docker buildx imagetools inspect ghcr.io/sourceblender/musubi-core:<version>`),
then verify it:

```bash
cosign verify \
  --certificate-identity-regexp '^https://github\.com/(ericmey|sourceblender)/musubi/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/sourceblender/musubi-core@sha256:<digest>
```

`group_vars/all.yml` carries the current release's pin, updated by an
automatic PR after each release.

### Secrets and settings

[`deploy/docker/.env.production.example`](../../deploy/docker/.env.production.example)
lists every setting Core reads. Two are secrets and belong in your secret
manager (with Ansible, the encrypted `vault.yml`), never in the repo or in
shell history:

- `JWT_SIGNING_KEY`: signs and verifies agent tokens (see
  [Connect](connect.md)). Use a long random value; a key that looks like a PEM
  or JSON public key is rejected.
- `QDRANT_API_KEY`: authenticates Core to Qdrant.

`OAUTH_AUTHORITY` is the token issuer: every token's `iss` claim must match
it. Models, ports and data paths have working defaults.

### Check it

On the host, once the stack is up:

```bash
curl -fsS http://127.0.0.1:8100/v1/ops/health
```

Set up backups before you rely on it: see [Operate](operate.md#backups). Put
TLS in front of Core (the runbook uses a gateway) before any agent reaches it
over a network you don't fully control.
