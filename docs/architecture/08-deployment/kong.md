---
title: Kong API Gateway
section: 08-deployment
tags: [deployment, gateway, kong, section/deployment, status/complete, tls, type/spec]
type: spec
status: complete
updated: 2026-04-18
up: "[[08-deployment/index]]"
reviewed: true
---

# Kong API Gateway

The VLAN-wide API gateway. Fronts Musubi Core's public API surface; terminates TLS; enforces edge rate-limits, basic auth, and access logging. Runs on a dedicated VM (`<kong-gateway>`, `<kong-ip>`) — **not on the Musubi host itself**. See [[13-decisions/0014-kong-over-caddy]] for the rationale.

> Concrete hostnames, IPs, and domains in this spec use placeholder tokens (`<kong-gateway>`, `<musubi-host>`, `<homelab-domain>`, etc.). The real values for the operator's deployment live in `.agent-context.local.md` at the repo root, which is gitignored. See that file's **Placeholder → real-value map** before running any command that needs a concrete endpoint.

## Topology

```
                        ┌─────────────────────────────────────┐
   Client on LAN  ────▶ │  Kong (<kong-gateway>, <kong-ip>)    │
   (Claude Code,        │                                      │
    LiveKit, user's     │  :443   TLS terminus                 │
    laptop, shell)      │         host-header routing          │
                        └──────────────┬──────────────────────┘
                                       │
                                       │ http://<musubi-ip>:8100
                                       ▼
                        ┌──────────────────────────┐
                        │ Musubi Core (Musubi host) │
                        │ Docker Compose stack      │
                        │ Inference on Docker bridge│
                        └──────────────────────────┘
```

**Only Kong faces the LAN.** The Musubi host exposes exactly one port to Kong: `<musubi-ip>:8100` (plain HTTP, Musubi Core). Everything else on the Musubi host stays inside the Compose bridge network.

## What Kong owns

- **TLS termination** with certs managed by Kong (Let's Encrypt via ACME or internal CA).
- **Edge auth** — bearer-token validation, OAuth flow for the MCP HTTP transport, per-route allow/deny lists.
- **Rate limiting** — per-IP and per-consumer.
- **Access logging** — structured JSON to wherever Kong's log sink points.
- **Host-header routing** — multiple services fronted by the same Kong instance.

## What Kong does **not** own

- **Per-tenant / per-namespace authorization.** That's Musubi Core's job (it owns the canonical API schema; Kong just forwards a validated token).
- **Request body semantics, validation, or shaping.** Those live in Musubi Core.
- **Qdrant / TEI / Ollama exposure.** Those are bridge-only inside the Musubi host's Compose; Kong never reaches them.

## Routes Kong serves for Musubi

### `<musubi-host>`

The canonical API. One upstream, bearer-token required, rate-limited.

| Route         | Upstream                            | Notes                               |
|---------------|-------------------------------------|-------------------------------------|
| `/v1/*`       | `http://<musubi-ip>:8100/v1/*`      | All client-facing routes            |
| `/oauth/*`    | `http://<musubi-ip>:8100/oauth/*`   | MCP OAuth flow (handled by Core)    |
| `/healthz`    | `http://<musubi-ip>:8100/healthz`   | Liveness; unauth                    |

### `ollama.<homelab-domain>` (optional)

If the operator wants Ollama reachable for non-Musubi general-purpose LLM work. Separate route, separate auth.

| Route  | Upstream                           | Notes                               |
|--------|------------------------------------|-------------------------------------|
| `/*`   | `http://<musubi-ip>:11434/*`       | Raw Ollama API; token-auth required |

### Not fronted by Kong

- Qdrant, TEI (dense / sparse / rerank), and any internal Musubi containers. Admin access goes via `ssh <musubi-host>` + `docker exec` or an SSH tunnel when debugging.

## Configuration pattern

Kong is configured either declaratively (`kong.yaml` / deck) or via its Admin API. Either way, the Musubi-relevant pieces:

```yaml
# Illustrative decK config; adapt to your Kong deployment
services:
  - name: musubi-core
    url: http://<musubi-ip>:8100
    retries: 2
    connect_timeout: 2000
    write_timeout: 30000
    read_timeout: 30000
    routes:
      - name: musubi-api-v1
        hosts: ["<musubi-host>"]
        paths: ["/v1", "/oauth", "/healthz"]
        protocols: [https]
        strip_path: false
    plugins:
      - name: rate-limiting
        config:
          minute: 300
          policy: local
      - name: cors
        config:
          origins: ["https://claude.ai", "http://localhost:*"]
          credentials: true
      - name: request-transformer
        config:
          add:
            headers: ["X-Forwarded-Proto:https"]
      - name: file-log
        config:
          path: /var/log/kong/musubi-api.log
          reopen: true

  - name: ollama
    url: http://<musubi-ip>:11434
    routes:
      - name: ollama-api
        hosts: ["ollama.<homelab-domain>"]
        paths: ["/"]
        protocols: [https]
    plugins:
      - name: key-auth
        config:
          key_names: ["X-API-Key"]
      - name: rate-limiting
        config:
          minute: 60
```

Plug in whatever your Kong deployment uses (Kong Gateway OSS, Enterprise, or Konnect). Musubi Core doesn't care which; it only expects the upstream HTTP traffic to carry a `Authorization: Bearer <token>` header that Core can parse.

## Token handoff

- Kong validates the bearer token format and (optionally) JWT signature. If signed: Kong rejects invalid tokens before they touch Musubi.
- Kong forwards the token to Musubi Core unchanged via `Authorization:` header.
- Musubi Core re-parses the token, resolves it against its own tenant/presence model, and enforces namespace scope.
- **Don't rely on Kong alone for auth.** Musubi re-validates; the two checks are complementary. See [[10-security/auth]].

## TLS

- Certs managed by Kong. For a `<homelab-domain>` zone, either:
  - **Let's Encrypt** with DNS-01 against whatever DNS provider hosts the domain, or
  - **Internal CA** (e.g. `step-ca`) if the domain is split-horizon.
- Musubi Core itself serves plain HTTP. Never expose Core's `:8100` to anything other than Kong.

## Musubi host firewall

With Kong as the gateway, the Musubi host's ingress policy is:

| Port | Allow from                    | Why                                          |
|------|-------------------------------|----------------------------------------------|
| 22   | admin subnet                  | SSH for ops                                  |
| 8100 | `<kong-gateway>` (`<kong-ip>/32`) | Kong upstream — Musubi Core API              |

No other LAN ingress. `ufw` rules codified in [[08-deployment/ansible-layout#firewall]].

## Failure modes

- **Kong down** → Musubi unreachable from clients even if healthy. Musubi Core keeps running; queued writes from adapters succeed when Kong recovers (adapters retry).
- **Musubi Core down** → Kong returns 502 with a JSON error body; clients see `{"code":"upstream_unavailable", …}`. Adapter SDKs retry with exponential backoff.
- **TLS cert expiry** → Kong's ACME renewal handles this; stale certs surface in Kong's health dashboard.
- **`<kong-gateway>` / Kong VM destroyed** → rebuild from Ansible on `<pve-node-1>` following the standard VM-clone pattern ([[_slices/slice-adapter-mcp]] pattern is analogous). Kong config restores from declarative source (git). Musubi itself is unaffected.

## Related

- [[13-decisions/0014-kong-over-caddy]] — why Kong and not Caddy.
- [[07-interfaces/canonical-api]] — what Kong routes to.
- [[10-security/auth]] — layered auth: Kong edge + Core deep.
- [[08-deployment/compose-stack]] — what lives behind the gateway.
