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

Production is the same shape on one host: Musubi Core, Qdrant, three
text-embeddings-inference services (dense, sparse, reranker) and Ollama, on
one Docker network. Only Core publishes a port.

1. **Pick an image by digest.** Images are published to
   `ghcr.io/sourceblender/musubi-core`, signed with cosign, scanned, and
   carry an SBOM. Tags move and digests don't, so pin the digest from the
   release you want (its GitHub Release page, or
   `docker buildx imagetools inspect ghcr.io/sourceblender/musubi-core:<version>`),
   then verify it:

   ```bash
   cosign verify \
     --certificate-identity-regexp '^https://github\.com/(ericmey|sourceblender)/musubi/.*' \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com \
     ghcr.io/sourceblender/musubi-core@sha256:<digest>
   ```

2. **Start from the Compose stack.** [`docker-compose.yml`](../../docker-compose.yml)
   is the canonical stack; [`deploy/docker/README.md`](../../deploy/docker/README.md)
   describes the files and the start-up flow. **Set `services.core.image` to
   the digest you verified.** The committed value is a placeholder, and the
   stack won't start until you replace it.

3. **Configure it.** Copy
   [`deploy/docker/.env.production.example`](../../deploy/docker/.env.production.example)
   to `.env.production` and fill it in. Two values are secrets and belong in
   your secret manager, not in the repo or in shell history:
   - `JWT_SIGNING_KEY`: signs and verifies agent tokens (see
     [Connect](connect.md)). Use a long random value; a key that looks like a
     PEM or JSON public key is rejected.
   - `QDRANT_API_KEY`: authenticates Core to Qdrant.

   `OAUTH_AUTHORITY` is the token issuer: every token's `iss` claim must match
   it. Models, ports and data paths have working defaults in the example.

4. **Start and check it.**

   ```bash
   docker compose --env-file .env.production config --quiet
   docker compose --env-file .env.production up -d
   curl -fsS http://127.0.0.1:8100/v1/ops/health
   ```

5. **Set up backups before you rely on it.** See [Operate](operate.md#backups).

For an Ansible-managed host (rendered Compose file, systemd unit, credential
preflight before every deploy), use [`deploy/ansible/`](../../deploy/ansible/README.md)
and the [first-deploy runbook](../../deploy/runbooks/first-deploy.md).

Put TLS in front of Core (a reverse proxy or gateway) before any agent
reaches it over a network you don't fully control.
