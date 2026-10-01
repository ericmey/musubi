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

Core listens on `127.0.0.1:8100` by default (`MUSUBI_CORE_BIND`,
`MUSUBI_CORE_PORT`), so a fresh install is reachable only from the host itself.
Setting `MUSUBI_CORE_BIND` to a LAN address is a choice you make deliberately,
and you own what protects it: put TLS and access control in front of Core
before any agent reaches it over a network you don't fully control. No model
service publishes a port on the host.

### Start the stack

```bash
git clone https://github.com/sourceblender/musubi && cd musubi
cp .env.example .env && chmod 600 .env   # .env is gitignored; fill in the values below
```

`.env.example` leaves the required values blank on purpose. Fill them all in
before you run `docker compose config` or `up`: the secrets (see
[Secrets and settings](#secrets-and-settings)) and where the models are:

- **Remote models (the default stack):** `TEI_DENSE_URL`, `TEI_SPARSE_URL`,
  `TEI_RERANKER_URL` and `OLLAMA_URL`, plus `TEI_BASIC_AUTH_USERNAME` and
  `TEI_BASIC_AUTH_PASSWORD` if your endpoints need them. Then:

  ```bash
  docker compose up -d --wait
  ```

  This runs Core, the lifecycle worker and Qdrant, with data in named Docker
  volumes.

- **Models on this host's GPU:** add the GPU override, which also runs the three
  embedding services and Ollama. Pin both of its images in `.env`, choosing the
  versions yourself:

  - `MUSUBI_TEI_IMAGE`: the text-embeddings-inference image and a tag built for
    your GPU's architecture, as `repo:tag`;
  - `MUSUBI_TEI_DIGEST`: that image's digest, 64 hex characters, without the
    `sha256:` prefix;
  - `MUSUBI_OLLAMA_IMAGE` and `MUSUBI_OLLAMA_DIGEST`: the same for Ollama.

  Read a digest with
  `docker buildx imagetools inspect <repo:tag> --format '{{json .Manifest.Digest}}'`
  and drop the quotes and the `sha256:` prefix.
  The override refuses to start without all four, so it never runs an
  unpinned image.

  ```bash
  docker compose -f docker-compose.yml -f deploy/docker/compose.local-gpu.yml up -d --wait
  ```

  The override starts Ollama but does not download a model. Pull the one
  `LLM_MODEL` names in `.env`:

  ```bash
  docker compose -f docker-compose.yml -f deploy/docker/compose.local-gpu.yml \
    exec ollama ollama pull "$(sed -n 's/^LLM_MODEL=//p' .env)"
  ```

The lifecycle jobs use Ollama by default. To use an OpenAI-compatible endpoint
instead, set `LIFECYCLE_LLM_API`, `LIFECYCLE_LLM_BASE_URL`,
`LIFECYCLE_LLM_MODEL` and `LIFECYCLE_LLM_API_KEY`.

### Pin and verify the image

Images are published to `ghcr.io/sourceblender/musubi-core`, signed with
cosign, scanned, and carry an SBOM. Tags move and digests don't, so pin the
digest from the release you want (its GitHub Release page, or
`docker buildx imagetools inspect ghcr.io/sourceblender/musubi-core:<version>`),
then verify it:

```bash
cosign verify \
  --certificate-identity 'https://github.com/sourceblender/musubi/.github/workflows/publish-core-image.yml@refs/tags/<version>' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/sourceblender/musubi-core@sha256:<digest>
```

`<version>` is the release tag you are installing (for example `v1.27.12`), so
the signature must come from exactly that release's publish workflow.

The root `docker-compose.yml` carries the current release's pin on one shared
image line (the `x-core-image` anchor), used by Core, the lifecycle worker and
the one-shot volume setup. An automatic PR updates that line, and the
quickstart's, after each release.

### Secrets and settings

[`.env.example`](../../.env.example) lists the settings a Compose deployment
needs; Core reads a few more advanced ones, documented in its settings
reference. Two secrets are mandatory:

- `JWT_SIGNING_KEY`: signs and verifies agent tokens (see
  [Connect](connect.md)). Use a long random value; a key that looks like a PEM
  or JSON public key is rejected.
- `QDRANT_API_KEY`: authenticates Core to Qdrant.

Every credential field belongs in your secret manager or your private `.env`,
never in the repo or in shell history. That includes the optional ones, such as
`TEI_BASIC_AUTH_PASSWORD` and `LIFECYCLE_LLM_API_KEY`.

`OAUTH_AUTHORITY` is the token issuer: every token's `iss` claim must match
it. Core's bind address and port, the model names and the data volumes have
defaults; the secrets and the model endpoint URLs do not.

### Check it

On the host, once the stack is up:

```bash
curl -fsS http://127.0.0.1:8100/v1/ops/health
```

Set up backups before you rely on it: see [Operate](operate.md#backups). Put
TLS in front of Core before any agent reaches it
over a network you don't fully control.
