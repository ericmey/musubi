<p align="center">
  <h1 align="center">Musubi 結び</h1>
  <p align="center">
    <em>Shared memory for a small fleet of AI agents — three planes, local inference, a lifecycle engine that matures raw captures into a human-reviewable knowledge base.</em>
  </p>
  <p align="center">
    <a href="https://github.com/sourceblender/musubi/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/sourceblender/musubi?sort=semver&color=blue"></a>
    <a href="https://github.com/sourceblender/musubi/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/sourceblender/musubi/actions/workflows/ci.yml/badge.svg?branch=main"></a>
    <a href="https://github.com/sourceblender/musubi/actions/workflows/publish-core-image.yml"><img alt="Signed image" src="https://github.com/sourceblender/musubi/actions/workflows/publish-core-image.yml/badge.svg?branch=main"></a>
    <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-blue"></a>
    <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-blue">
    <img alt="cosign signed" src="https://img.shields.io/badge/cosign-signed-brightgreen">
  </p>
</p>

---

Musubi (結び — *"to tie, to join, to bind"*) is a memory server built for the moment when a single AI assistant is not enough: you're running several, each with its own role — one drafts notes, one answers questions, one cleans up the vault at 3am — and they need a shared substrate so that what one learns, the others can use.

It is a standalone Python service with an HTTP API. Agent integrations (Claude Code, Codex, OpenClaw, LiveKit and others) are separate plugins that talk to that API; the core owns the memory model, the plugins own the surface.

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

A **lifecycle engine** runs the sweeps on a schedule: episodic maturation and provisional expiry (hourly), synthesis (03:00), concept maturation (03:30), promotion (04:00), concept demotion (05:00), reflection (06:00, writes a daily digest to the vault), episodic demotion (weekly, Sunday 03:45) and a vault reconcile every six hours. Each sweep is file-locked, idempotent, and records its events in a SQLite journal.

## Why not a single RAG index?

One reason: **lifecycle**. A plain vector store keeps everything forever and retrieves by cosine. Musubi's episodic rows expire on a TTL if they aren't matured; mature ones flow up through synthesis; promoted ones become curated rows the human owns. Nothing sits in a "everything I've ever said" bucket — the system makes opinions about what's worth keeping and surfaces them for review.

The other reason: **agent ↔ agent memory**. Thoughts are a first-class message channel — agents can `send` and `subscribe` without a coordinator process. It's not a chat log, it's a shared board of short-lived state.

Design choices are captured as ADRs in [`docs/Musubi/13-decisions/`](docs/Musubi/13-decisions/).

## Stack

- **Python 3.12**, `pydantic v2`, strict `mypy`, `ruff` format + lint.
- **Qdrant** for named-vector hybrid search (dense + sparse + rerank).
- **TEI (text-embeddings-inference)** for BGE-M3 dense + SPLADE sparse + BGE-reranker — all GPU-hostable, CPU-fallback OK.
- **Ollama** for LLM calls (maturation scoring, synthesis, promotion rendering, reflection). You choose the model with `LLM_MODEL`; the examples use `qwen3.5:9b`, and any Ollama tag works. The lifecycle sweeps can use an OpenAI-compatible endpoint instead.
- **FastAPI** HTTP/JSON API. That is the only wire protocol.
- **JWT bearer tokens** (HS256 or RS256) with per-namespace scopes.
- **Docker Compose** for a laptop or a single production host. Every published image is [cosign](https://github.com/sigstore/cosign)-signed by digest, Trivy-scanned, and ships with a CycloneDX SBOM attestation.

## Try it

The whole memory server on CPU, with Docker and nothing else:

```bash
git clone https://github.com/sourceblender/musubi && cd musubi
docker compose -f quickstart/docker-compose.yml up -d --wait   # first boot downloads ~2.5 GB of models
docker compose -f quickstart/docker-compose.yml run --rm demo
```

The demo has one agent capture memories, then a second agent with a
**read-only** token recall the right one from a question that shares no
content words with it, and finally checks that the read-only token is
refused a write. It exits non-zero if any of that fails, and the
[Quickstart workflow](.github/workflows/quickstart.yml) runs it on a clean
runner. The quickstart uses smaller CPU models than production; see
[`quickstart/docker-compose.yml`](quickstart/docker-compose.yml).
The API is then on `http://127.0.0.1:8100/v1`.

To work on the code (Python 3.12 + [uv](https://docs.astral.sh/uv/)):

```bash
make install
make check    # lint, types, and the unit suite
```

**Running it for real, connecting your agents, and operating it:** see the
[user guide](docs/guide/README.md). It covers the single-box Compose stack, image pinning
and signature checks, tokens, every agent plugin, upgrades and backups.

## Repository layout

```
src/musubi/                 importable package
  types/                    shared pydantic types — the schema is the contract
  api/                      FastAPI app and the /v1 routes
  auth/                     JWT validation, scopes, credential preflight
  planes/                   episodic / concept / curated / artifact / thoughts
  ingestion/                capture service (dedup, idempotency, retry); not yet used by the HTTP routes
  retrieve/                 scoring, hybrid search, fast/deep paths
  lifecycle/                maturation / synthesis / promotion / demotion / reflection / scheduler
  llm/                      Ollama and OpenAI-compatible clients, versioned prompt files
  embedding/                TEI client, Embedder protocol, FakeEmbedder
  store/ storage/           Qdrant layout, collection specs, client factories
  vault/                    Obsidian watcher, writer, reconciler, write-log
  sdk/                      Python client (sync and async)
  adapters/                 in-repo MCP server and LiveKit adapter code
  cli/                      the `musubi` operator CLI
  ops/                      retention and cleanup jobs
  evals/                    retrieval-quality evals and the live quality gate
  observability/            structured logging, metrics, tracing

tests/                      mirrors src/musubi/
quickstart/                 CPU-only Compose stack and the demo
docs/guide/                 user guide: install, connect agents, use, operate
docs/Musubi/                architecture docs and ADRs (Obsidian-style vault)
deploy/                     Compose overrides, Prometheus and Alertmanager config, smoke checks, test env
```

## Status

**v1, in production use.** The current release is on the [releases page](https://github.com/sourceblender/musubi/releases/latest) and in [`CHANGELOG.md`](CHANGELOG.md).

What ships today:

- Three-plane memory (episodic / concept / curated) plus artifacts and thoughts, with per-plane collections and KSUID-addressed rows
- The lifecycle sweeps above, journaled in SQLite, with a three-strikes rejection cap on promotion
- Hybrid retrieval: dense BGE-M3, sparse SPLADE and a BGE reranker, across planes in one call
- Plane-aligned HTTP endpoints (`/v1/episodic`, `/v1/curated`, `/v1/concepts`, `/v1/artifacts`, `/v1/thoughts`) and a Python SDK
- Agent-as-tenant namespaces, `<agent>/<channel>/<plane>` ([ADR 0030](docs/Musubi/13-decisions/0030-agent-as-tenant.md))
- `Last-Event-ID` replay on `/v1/thoughts/stream`, so a reconnecting agent does not lose thoughts
- Signed, scanned images with an SBOM, released through reviewed release PRs; digest pins are applied by an operator, never automatically
- Operator CLI: `musubi promote force|reject`

Open work is tracked in [GitHub issues](https://github.com/sourceblender/musubi/issues). Longer-term direction is in [`docs/Musubi/12-roadmap/`](docs/Musubi/12-roadmap/).

## Contributing

This is a personal project that's been opened up for others to follow along, fork, and riff on. Contributions are welcome but the bar is: opened issue → discussion → PR. See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and conventions.

The internal design is captured in an Obsidian-style vault at [`docs/Musubi/`](docs/Musubi/). It's readable as-is on GitHub but renders best in Obsidian. Every architectural decision has an ADR, and each architecture spec carries a Test Contract naming the tests that prove it.

## Security

If you find a vulnerability, please don't open a public issue. See [SECURITY.md](SECURITY.md) for the disclosure process.

## License

[Apache 2.0](LICENSE) © 2025–2026 Eric Mey.
