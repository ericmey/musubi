<p align="center">
  <h1 align="center">Musubi 結び</h1>
  <p align="center">
    <em>Shared memory for a small fleet of AI agents — three planes, local inference, a lifecycle engine that matures raw captures into a human-reviewable knowledge base.</em>
  </p>
  <p align="center">
    <a href="https://github.com/ericmey/musubi/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/ericmey/musubi?sort=semver&color=blue"></a>
    <a href="https://github.com/ericmey/musubi/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/ericmey/musubi/actions/workflows/ci.yml/badge.svg?branch=main"></a>
    <a href="https://github.com/ericmey/musubi/actions/workflows/publish-core-image.yml"><img alt="Signed image" src="https://github.com/ericmey/musubi/actions/workflows/publish-core-image.yml/badge.svg?branch=main"></a>
    <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-blue"></a>
    <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-blue">
    <img alt="cosign signed" src="https://img.shields.io/badge/cosign-signed-brightgreen">
  </p>
</p>

---

Musubi (結び — *"to tie, to join, to bind"*) is a memory server built for the moment when a single AI assistant is not enough: you're running several, each with its own role — one drafts notes, one answers questions, one cleans up the vault at 3am — and they need a shared substrate so that what one learns, the others can use.

It is a standalone Python service. Every downstream interface (MCP, LiveKit, a CLI, a browser extension) is an adapter that depends on Musubi's SDK. The core owns the memory model and the API; adapters own the surface.

## The three planes

```
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                            MUSUBI CORE                                   │
  │                                                                          │
  │    episodic ──────► concept ──────► curated                              │
  │    (raw captures)   (synthesized    (human-reviewed                      │
  │                      themes)         Obsidian notes)                     │
  │                                                                          │
  │    + artifact plane (binary blobs — images, audio, pdfs)                 │
  │    + thoughts plane (pub/sub channel for agent ↔ agent messaging)        │
  │                                                                          │
  └──────────────────────────────────────────────────────────────────────────┘
```

- **Episodic** — every sentence, quote, and observation an agent ingests. Written fast, scored by an LLM for importance, matured to `matured` after a dwell window if no contradictions surface.

- **Concept** — a daily pass clusters mature episodics by shared topic + semantic similarity, asks an LLM to summarise each cluster into a `SynthesizedConcept`, checks for contradictions between concepts, and persists the result. Concepts reinforce (not duplicate) when similar clusters recur.

- **Curated** — concepts that clear a promotion gate (reinforcement count + importance + age) are rendered as markdown and written to an [Obsidian](https://obsidian.md) vault. A human reviewer sees them, edits them, moves them around. Edits flow back into Musubi through the vault sync.

A **lifecycle engine** runs five sweeps on cron: maturation (hourly), synthesis (03:00), promotion (04:00), demotion (05:00, sweep unreinforced rows out), reflection (06:00, writes a daily digest back to the vault). Each sweep is file-locked, idempotent, and emits structured events to a SQLite journal.

## Why not a single RAG index?

One reason: **lifecycle**. A plain vector store keeps everything forever and retrieves by cosine. Musubi's episodic rows expire on a TTL if they aren't matured; mature ones flow up through synthesis; promoted ones become curated rows the human owns. Nothing sits in a "everything I've ever said" bucket — the system makes opinions about what's worth keeping and surfaces them for review.

The other reason: **agent ↔ agent memory**. Thoughts are a first-class message channel — agents can `send` and `subscribe` without a coordinator process. It's not a chat log, it's a shared board of short-lived state.

Design choices are captured as ADRs in [`docs/Musubi/13-decisions/`](docs/Musubi/13-decisions/).

## Stack

- **Python 3.12**, `pydantic v2`, strict `mypy`, `ruff` format + lint.
- **Qdrant** for named-vector hybrid search (dense + sparse + rerank).
- **TEI (text-embeddings-inference)** for BGE-M3 dense + SPLADE sparse + BGE-reranker — all GPU-hostable, CPU-fallback OK.
- **Ollama** for LLM calls (maturation scoring, synthesis, promotion rendering, reflection). Defaults to Qwen 2.5 7B; any Ollama-tagged model works.
- **FastAPI + HTTPX** HTTP surface; **gRPC** generated from `proto/` (partial). Both exposed on the same port.
- **Docker Compose** for local / single-box deploy. **Ansible** playbooks for a managed-host rollout (`deploy/ansible/`). Every published image is [cosign](https://github.com/sigstore/cosign)-signed by digest, Trivy-scanned, and ships with a CycloneDX SBOM attestation.

## Try it

```bash
# 1. Clone
git clone https://github.com/ericmey/musubi && cd musubi

# 2. Install (Python 3.12 + uv required — https://docs.astral.sh/uv/)
make install

# 3. Run the local test suite
make check
```

A single-box Docker Compose deploy is laid out in [`deploy/ansible/templates/docker-compose.yml.j2`](deploy/ansible/templates/docker-compose.yml.j2); a first-deploy runbook lives at [`deploy/runbooks/upgrade-image.md`](deploy/runbooks/upgrade-image.md). The published image is:

```
ghcr.io/ericmey/musubi-core:v0.3.0
```

Verify the signature before pinning in production:

```bash
cosign verify \
  --certificate-identity-regexp 'https://github.com/ericmey/musubi/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/ericmey/musubi-core:v0.3.0
```

## Repository layout

```
src/musubi/                 importable package
  types/                    shared pydantic types — the schema is the contract
  store/                    Qdrant layout, collection names, vector specs
  embedding/                TEI client + Embedder protocol + FakeEmbedder
  planes/                   episodic / concept / curated / artifact / thoughts
  retrieve/                 scoring, hybrid search, fast/deep paths
  lifecycle/                maturation / synthesis / promotion / demotion / reflection / runner
  llm/                      Ollama client + frozen prompt files (per-name versioned)
  api/                      FastAPI app, OpenAPI, /v1/* routes
  sdk/                      Python client
  adapters/                 MCP, LiveKit, OpenClaw (SDK + types only)
  vault/                    Obsidian watcher + writer + write-log
  observability/            structured logging + Prometheus metrics

tests/                      mirrors src/musubi/ path-for-path
docs/Musubi/                the architecture vault (Obsidian) — source of truth for design
deploy/                     ansible, prometheus, grafana, docker-compose templates
```

## Status

**v0.3.0** (released 2026-04-21) — the first public release. Feature-complete for:

- ✅ All five lifecycle sweeps running in production (maturation, synthesis, promotion, demotion, reflection)
- ✅ Hybrid retrieval (dense BGE-M3 + sparse SPLADE + BGE-reranker)
- ✅ Full HTTP/gRPC API surface
- ✅ MCP + LiveKit adapter first cuts
- ✅ Supply-chain: cosign + SBOM + Trivy on every published image
- ✅ Homelab deployment wired, containers healthy

Not yet:
- ⏳ Fleet orchestration (V2 runs single-node today; multi-node is post-v1.0)
- ⏳ Auto-deploy pipeline
- ⏳ OpenClaw adapter integration tests

Roadmap detail lives in [`docs/Musubi/12-roadmap/`](docs/Musubi/12-roadmap/).

## Contributing

This is a personal project that's been opened up for others to follow along, fork, and riff on. Contributions are welcome but the bar is: opened issue → discussion → PR. See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and conventions.

The internal design is captured in an Obsidian-style vault at [`docs/Musubi/`](docs/Musubi/). It's readable as-is on GitHub but renders best in Obsidian. Every architectural decision has an ADR; every working module has a `slice` spec with a Test Contract.

## Security

If you find a vulnerability, please don't open a public issue. See [SECURITY.md](SECURITY.md) for the disclosure process.

## License

[Apache 2.0](LICENSE) © 2025–2026 Eric Mey.
