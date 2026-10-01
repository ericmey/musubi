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

Musubi installs as a Docker Compose stack on a machine you have already set up.
It does not install Docker, GPU drivers or packages, and it never changes your
firewall: the host is yours.

### What the host needs

- **Linux with Docker Engine and Docker Compose v2.** Install and update them
  the way you manage the rest of the machine.
- **Somewhere to run the models.** Core needs a dense embedder, a sparse
  embedder and a reranker, plus an LLM for the lifecycle jobs. Run them either:
  - **on this host, on an NVIDIA GPU:** a working driver and the
    [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)
    configured for Docker; or
  - **elsewhere:** any reachable text-embeddings-inference compatible endpoints
    and an OpenAI-compatible or Ollama LLM endpoint, set by URL.
- **Disk and memory for Qdrant and the models.** For sizing, the docs' latency
  and capacity figures were measured on a reference host: Ryzen 5, 32 GB RAM,
  RTX 3080 10 GB. It is a point of comparison, not a requirement. See
  [capacity](../Musubi/09-operations/capacity.md).

### Network exposure

Core listens on `127.0.0.1` by default, so a fresh install is reachable only
from the host itself. Exposing it on a network is an explicit setting you
choose, and you own what protects it: put TLS and access control in front of
Core before any agent reaches it over a network you don't fully control.

<!-- TODO(compose-first): steps to fetch, configure and start the production
Compose stack go here once its path, env file and bind settings are final. -->

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

<!-- TODO(compose-first): name the file that carries the current release's
pin once the release PR targets the production Compose stack. -->

### Secrets and settings

[`deploy/docker/.env.production.example`](../../deploy/docker/.env.production.example)
lists every setting Core reads. Two are secrets and belong in your secret
manager, never in the repo or in shell history:

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
