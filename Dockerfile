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
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/uv-cache \
    uv sync --frozen --no-install-project --no-dev

# Now install the project itself.
COPY src/ ./src/
COPY README.md ./
RUN --mount=type=cache,target=/uv-cache \
    uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm AS runtime

# Non-root user to run the service.
#
# UID/GID match the `musubi` system user that Ansible's `bootstrap.yml` creates
# on the target host (uid=999 gid=985). Keeping the in-container IDs aligned
# lets bind-mounts from the host data dirs (`/var/lib/musubi/*`, `/var/log/musubi`,
# 0750 perms owned by host `musubi`) be read and written without privilege
# escalation or chown gymnastics.
RUN groupadd --system --gid 985 musubi \
 && useradd  --system --uid 999 --gid 985 --home-dir /app --no-create-home musubi \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder --chown=musubi:musubi /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MUSUBI_BIND_HOST=0.0.0.0 \
    MUSUBI_PORT=8100

USER musubi

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS http://localhost:8100/v1/ops/health || exit 1

CMD ["uvicorn", "--factory", "musubi.api.app:create_app", \
     "--host", "0.0.0.0", "--port", "8100", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
