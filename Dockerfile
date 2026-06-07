# syntax=docker/dockerfile:1.9
#
# Musubi Core image.
#
# Multi-stage: `builder` installs deps via uv into a .venv; `runtime` copies
# the built venv + source into a slim base. No compilers or build tooling in
# the final image.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/uv-cache \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install production deps first so the layer caches when only source changes.
# `--extra otel` includes the OpenTelemetry SDK + OTLP exporter + FastAPI/
# logging instrumentation so the server can emit spans to Tempo when
# Settings.otel_exporter_otlp_endpoint is set. Per
# [[09-operations/observability]] § Tracing. When the endpoint is unset
# the deps are imported lazily and produce no overhead.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/uv-cache \
    uv sync --frozen --no-install-project --no-dev --extra otel

# Now install the project itself.
COPY src/ ./src/
COPY README.md ./
RUN --mount=type=cache,target=/uv-cache \
    uv sync --frozen --no-dev --extra otel


FROM python:3.12-slim-bookworm AS runtime

# Non-root user to run the service.
#
# UID/GID match the `musubi` system user that Ansible's `bootstrap.yml` creates
# on the target host (uid=999 gid=985). Keeping the in-container IDs aligned
# lets bind-mounts from the host data dirs (`/var/lib/musubi/*`, `/var/log/musubi`,
# 0750 perms owned by host `musubi`) be read and written without privilege
# escalation or chown gymnastics.
# `apt-get upgrade` security-patches packages baked into the base image
# (python:3.12-slim-bookworm) before it next republishes. The image's CI
# Trivy scan gates on CRITICAL CVEs with a fix available, and Debian point
# releases (e.g. libgnutls30 deb12u6 -> deb12u7) land faster than the base
# image rebuilds — without this the gate trips on a CVE we didn't introduce.
RUN groupadd --system --gid 985 musubi \
 && useradd  --system --uid 999 --gid 985 --home-dir /app --no-create-home musubi \
 && apt-get update \
 && apt-get -y upgrade \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder --chown=musubi:musubi /app /app

# Two ownership/cache invariants the rest of the runtime depends on:
#
# 1. `/app` itself must be musubi-owned. `WORKDIR /app` above creates the
#    dir as root, and the `--chown` on the COPY only applies to the copied
#    contents — the dir's own owner stays root unless explicitly chowned.
#    Without this any code that tries to write a sibling under /app (HF's
#    default `$HOME/.cache`, pip's cache, transient temp files) fails with
#    `PermissionError: [Errno 13]`. Production hit this when the embedding
#    chunker's `Tokenizer.from_pretrained` was first wired into the episodic
#    write path.
#
# 2. `HF_HOME` lives outside `/app` so the cache is independent of any
#    bind-mount applied at /app, and so the cache survives independently
#    of any future change to /app ownership. The dir is pre-created musubi-
#    writable; the `RUN` below (as user musubi) populates it at build time
#    so the runtime image has the tokenizers baked in and needs no network
#    on first use.
RUN chown musubi:musubi /app \
 && install -d -o musubi -g musubi /opt/musubi/hf-cache

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MUSUBI_BIND_HOST=0.0.0.0 \
    MUSUBI_PORT=8100 \
    HF_HOME=/opt/musubi/hf-cache

USER musubi

# Pre-cache the tokenizers the embedding chunker needs. SPLADE-v3 is the
# load-bearing one (ChunkedEmbedder counts tokens against it for the
# 512-token sparse-encoder cap); BGE-M3 is used by the artifact plane's
# MarkdownHeadingChunker for storage-side chunking. Baking both into the
# image means zero runtime HuggingFace Hub dependency.
#
# SPLADE-v3 lives in a gated HF repo (https://huggingface.co/naver/splade-v3)
# so the build needs an HF read token to pull it. The token is provided
# via a build secret (`docker build --secret id=hf_token,…`) — never as
# a build-arg or ENV — so it never enters an image layer. CI sets up the
# secret from the `HF_TOKEN` repository secret; for local builds:
#   docker build --secret id=hf_token,src=$HOME/.huggingface/token .
#
# `/opt/musubi/hf-cache` is created musubi-owned above, so it is writable
# at runtime — but no runtime download is intended. Production relies
# entirely on the baked tokenizers; runtime writes would only happen if
# code drifts (a new tokenizer added without updating this RUN). That
# class of drift should be caught in PR review, not papered over with
# runtime hedging.
# Retry the prefetch: HuggingFace Hub rate-limits shared CI-runner IPs with
# HTTP 429, which the hub client's own backoff (~24s) doesn't always outlast.
# 5 outer attempts with growing backoff give the per-IP window time to reset
# so a transient 429 self-heals within the build instead of failing it. A
# genuine auth/gating error (401/403) still fails fast on the first attempt.
RUN --mount=type=secret,id=hf_token,uid=999 \
    export HF_TOKEN="$(cat /run/secrets/hf_token)" \
           HUGGINGFACE_HUB_TOKEN="$(cat /run/secrets/hf_token)"; \
    for attempt in 1 2 3 4 5; do \
      python -c "from tokenizers import Tokenizer; Tokenizer.from_pretrained('naver/splade-v3'); Tokenizer.from_pretrained('BAAI/bge-m3')" && break; \
      if [ "$attempt" = 5 ]; then echo "tokenizer prefetch failed after 5 attempts" >&2; exit 1; fi; \
      echo "tokenizer prefetch attempt $attempt failed (transient HF Hub error, e.g. 429); backing off..."; \
      sleep $((attempt * 20)); \
    done

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS http://localhost:8100/v1/ops/health || exit 1

CMD ["uvicorn", "--factory", "musubi.api.app:create_app", \
     "--host", "0.0.0.0", "--port", "8100", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
