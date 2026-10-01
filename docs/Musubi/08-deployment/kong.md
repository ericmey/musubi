---
title: "Exposing Core (TLS)"
section: 08-deployment
tags: [deployment, gateway, section/deployment, status/complete, tls, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[08-deployment/index]]"
reviewed: false
implements: "docker-compose.yml"
---

# Exposing Core (TLS)

How to let agents on other machines reach Musubi Core. Musubi ships no gateway: TLS and
any edge controls live in a reverse proxy the operator already runs (nginx, Caddy,
Traefik, HAProxy, an API gateway, and so on).

## What Core serves

- **Plain HTTP** on `127.0.0.1:8100` by default (`MUSUBI_CORE_BIND`,
  `MUSUBI_CORE_PORT` in `.env`). Core never terminates TLS itself.
- **Liveness:** `GET /v1/ops/health`. Readiness per dependency: `GET /v1/ops/status`.
- The API is under `/v1/*`.

## Proxy rules

1. **Terminate TLS in the proxy** and forward to Core over plain HTTP. If the proxy runs
   on the same host, keep Core on `127.0.0.1`. If it runs elsewhere, set
   `MUSUBI_CORE_BIND` to an address the proxy can reach, and restrict that port to the
   proxy with your own firewall.
2. **Forward `Authorization` unchanged.** Core validates every bearer token and enforces
   namespace scopes itself; a proxy-side token check is an extra layer, never a
   replacement. See [[10-security/auth]].
3. **Optionally pass `X-Request-Id`.** Core reuses the value if present, mints one
   otherwise, and echoes it on the response.
4. **Never expose Qdrant, TEI or Ollama.** In the shipped stack they publish no host
   port; keep it that way.
5. **Keep the read-only ops routes off untrusted networks.** `/v1/ops/health`,
   `/v1/ops/status` and `/v1/ops/metrics` need no bearer token by design
   ([[13-decisions/0038-network-protect-read-only-ops-endpoints]]). Don't route
   `/v1/ops/status` and `/v1/ops/metrics` through a proxy that untrusted clients can
   reach.

## Failure modes

- **Proxy down:** Core keeps running but clients cannot reach it.
- **Core down or starting:** the proxy returns its own 502/503 until Core's health
  check passes.

## Test Contract

**Module under test:** root `docker-compose.yml` port binding
(`tests/ops/test_public_compose.py`) and the ops router boundary
(`tests/ops/test_sec008_ops_network_boundary.py`).

1. `test_public_compose_remote_mode_is_host_independent` — Core binds `127.0.0.1`; no
   other service publishes a port.
2. `test_public_compose_allows_explicit_lan_bind` — the bind is an explicit operator
   choice.
3. `test_read_only_ops_exception_stays_bounded` — only the read-only ops routes skip
   operator auth.
