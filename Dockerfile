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
# Retry the prefetch on TRANSIENT errors only. HuggingFace Hub rate-limits
# shared CI-runner IPs with HTTP 429, which the hub client's own backoff
# (~24s) doesn't always outlast; up to 5 attempts with growing backoff give
# the per-IP window time to reset so a transient 429 self-heals in-build.
# Auth/gating errors (gated repo, 401/403/404) are classified and fail
# IMMEDIATELY — no wasted retries, and no mislabeling a real auth problem as
# a 429. The retry lives in Python (not a shell loop) precisely so it can
# inspect the exception chain to make that transient-vs-fatal distinction.
RUN --mount=type=secret,id=hf_token,uid=999 \
    HF_TOKEN="$(cat /run/secrets/hf_token)" \
    HUGGINGFACE_HUB_TOKEN="$(cat /run/secrets/hf_token)" \
    python - <<'PY'
import sys
import time

from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError
from tokenizers import Tokenizer

MODELS = ("naver/splade-v3", "BAAI/bge-m3")
MAX_ATTEMPTS = 5


def _chain(exc):
    seen, out = set(), []
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        out.append(exc)
        exc = exc.__cause__ or exc.__context__
    return out


def _is_fatal(exc):
    """Auth / gating / missing-repo are not transient: don't retry them."""
    for e in _chain(exc):
        if isinstance(e, (GatedRepoError, RepositoryNotFoundError)):
            return True
        if getattr(getattr(e, "response", None), "status_code", None) in (401, 403, 404):
            return True
    return False


for attempt in range(1, MAX_ATTEMPTS + 1):
    try:
        for model in MODELS:
            Tokenizer.from_pretrained(model)
        sys.exit(0)
    except Exception as exc:
        if _is_fatal(exc):
            print(f"tokenizer prefetch: fatal auth/gating error, not retrying: {exc!r}", file=sys.stderr)
            raise
        if attempt == MAX_ATTEMPTS:
            print(f"tokenizer prefetch: transient error persisted after {attempt} attempts: {exc!r}", file=sys.stderr)
            raise
        print(f"tokenizer prefetch attempt {attempt} failed (transient, e.g. HF 429); backing off {attempt * 20}s...", file=sys.stderr)
        time.sleep(attempt * 20)
PY

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS http://localhost:8100/v1/ops/health || exit 1

CMD ["uvicorn", "--factory", "musubi.api.app:create_app", \
     "--host", "0.0.0.0", "--port", "8100", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
