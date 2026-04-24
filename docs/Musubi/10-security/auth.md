---
title: Auth
section: 10-security
tags: [auth, oauth, scopes, section/security, security, status/complete, tokens, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[10-security/index]]"
reviewed: false
implements: "docs/Musubi/10-security/"
---
# Auth

Authentication + authorization for Musubi. OAuth 2.1 for human/adapter flows; JWT bearer tokens validated at the edge.

## Model

Single authority. Every call carries a bearer token; every token has a scope list; every scope names a namespace glob + access level.

```
┌─────────┐                     ┌──────────────┐                  ┌──────────────┐
│ Adapter │───── 1. PKCE ─────▶ │   Auth       │                  │   Kong      │
│  (MCP,  │                     │  Authority   │                  │  (musubi     │
│ LiveKit,│◀─── 2. token ────── │  /oauth/*    │                  │   edge)      │
│ Openclaw│                     └──────────────┘                  └──────────────┘
└─────────┘                                                              │
    │                                                                    │
    └──── 3. Bearer <token> ────────────────────────────────────────────▶│
                                                                         │ 4. JWT validate
                                                                         ▼
                                                                   ┌──────────────┐
                                                                   │  Musubi Core │
                                                                   │  scope check │
                                                                   └──────────────┘
```

1. Adapter starts PKCE flow.
2. User logs in at the authority, approves scopes, token issued.
3. Adapter calls Musubi with `Authorization: Bearer <jwt>`.
4. Kong (or Core — implementation detail) validates JWT signature + exp. Core checks scope against the requested namespace.

## Token format

JWT, RS256 signed:

```json
{
  "iss": "https://auth.internal.example.com",
  "sub": "eric-claude-code",          // principal id
  "aud": "musubi",
  "iat": 1744892400,
  "exp": 1744896000,                   // 1h
  "jti": "abc-123",
  "scope": [
    "eric/claude-code:r",
    "eric/claude-code/*:rw",
    "eric/_shared/curated:r",
    "eric/_shared/artifact:rw"
  ],
  "presence": "eric/claude-code"
}
```

Key fields:

- `sub` — principal ID (per-adapter install).
- `scope` — array of namespace-scope entries (see below).
- `presence` — the presence this token speaks for; used for thought routing + default namespace resolution.
- `exp` — 1-hour lifetime.
- `jti` — unique; used for replay defense if we add a nonce store later.

## Scope syntax

```
<namespace-glob>:<access-level>
```

Namespace glob:

- `eric/claude-code/episodic` — exact.
- `eric/_shared/curated` — shared scope.
- `eric/*/episodic` — all of Eric's episodic (rare; operator scope).
- `**` — full access (operator only).

Access level:

- `r` — read (retrieve, get).
- `w` — write (capture, patch).
- `rw` — read + write.

Non-namespace scopes (special):

- `operator` — admin endpoints.

**Thoughts scopes** — there is **no** separate `thoughts:send` / `thoughts:check:<presence>` / `thoughts:history:<presence>` keyword scope. Every thoughts endpoint (send, check, read, history, stream) checks against the standard namespace-scope form. See [[07-interfaces/canonical-api#scope-by-endpoint]] for the full table; the short version:

- `POST /v1/thoughts/send` → 3-segment `<tenant>/<presence>/thought:w`
- `POST /v1/thoughts/check` / `/read` / `/history` → 3-segment `<tenant>/<presence>/thought:r`
- `GET /v1/thoughts/stream` → 2-segment `<tenant>/<presence>:r`

A token with `<tenant>/<presence>:r` + `<tenant>/<presence>/*:rw` covers every thoughts flow for that presence.

## Signing key

- Algorithm: RS256 (2048-bit RSA key).
- Private key lives at `/etc/musubi/jwt-signing-key.pem` (mode 0400, owned by `musubi` user).
- Public key embedded in Core's config; also published at `/.well-known/jwks.json` (read-only) for adapters that support JWKS discovery.

Rotation procedure in [[09-operations/runbooks#rotate-tokens]].

## Auth authority

Two options:

### Self-hosted (default)

A small FastAPI service (`musubi-auth`) runs on the same box:

- OAuth 2.1 endpoints: `/oauth/authorize`, `/oauth/token`, `/oauth/revoke`, `/oauth/introspect`.
- User store: single admin user, password hashed with Argon2id.
- Client registry: YAML file (`/etc/musubi/oauth-clients.yaml`) listing registered adapters + allowed redirect URIs + default scopes.
- Stores: sqlite `auth.sqlite` for PKCE pending flows + refresh tokens.

Enough for household. Not hardened for anonymous internet use.

### External IdP (optional)

Anything OIDC-compatible (Authelia, Keycloak, Auth0). Musubi verifies signature via JWKS URL; scopes come from the IdP.

## Client registry

```yaml
# /etc/musubi/oauth-clients.yaml
clients:
  - client_id: musubi-mcp
    redirect_uris: ["chrome-extension://<ext-id>/oauth/callback",
                    "http://localhost:<port>/oauth/callback"]
    allowed_scopes:
      - eric/claude-code:r
      - eric/claude-code/*:rw
      - eric/_shared/curated:r
    public: true    # PKCE only, no client secret

  - client_id: musubi-livekit
    redirect_uris: ["http://localhost:8200/oauth/callback"]
    allowed_scopes:
      - eric/livekit-voice:r
      - eric/livekit-voice/*:rw
      - eric/_shared/curated:r
      - eric/_shared/concept:r
      - eric/_shared/artifact:rw
    public: true

  - client_id: musubi-openclaw
    redirect_uris: ["chrome-extension://<ext-id>/oauth/callback"]
    allowed_scopes:
      - eric/openclaw:r
      - eric/openclaw/*:rw
      - eric/_shared/curated:r
    public: true
```

No client secrets — PKCE is mandatory.

## Validation pipeline

On each request Core:

1. Parse `Authorization: Bearer <jwt>`. 401 if missing.
2. Verify signature with public key. 401 on mismatch.
3. Check `iss`, `aud`, `exp`, `nbf`. 401 on mismatch / expired.
4. Check `jti` against revocation list (cached from auth authority). 401 if revoked.
5. Extract `scope` + `presence`.
6. Per-endpoint scope check:
   - Capture → `<namespace>:w`.
   - Retrieve → `<namespace>:r` (or read any of the planes named in the query).
   - Thought send → `<tenant>/<presence>/thought:w` + recipient must be a known presence.
   - Thought check / read / history → `<tenant>/<presence>/thought:r`.
   - Thought stream (SSE) → `<tenant>/<presence>:r` (2-segment).
   - Operator endpoint → `operator`.
7. If check fails, 403 with structured error.

All this is in `musubi/auth/middleware.py`. It's a FastAPI dependency; every route carries it.

## Scope checks on retrieval

Retrieval is trickier — a query might span namespaces (blended). Rule:

- The `namespace` in the query (the address) determines access.
- If the namespace is a blended address (`eric/_shared/blended`), the token must have read access to the underlying planes it expands to (see [[05-retrieval/blended]]).

For opaque clients, the simplest pattern: token has `eric/_shared/blended:r` and Core handles the fanout. But the underlying plane reads are also scope-checked internally — defense in depth.

## Refresh tokens

- Issued with `offline_access` scope.
- 30-day lifetime, rotated on each use.
- Stored server-side (auth authority), encrypted.
- Revocable via `/oauth/revoke` or the CLI.

## Sign-out

Adapter calls `/oauth/revoke` with the refresh token. Authority deletes it. Access tokens still valid until expiry (up to 1h); adapters clear their local copy immediately.

Core learns about revoked tokens via a short-TTL cache refresh (60s) or `/oauth/introspect` for per-request validation (costlier).

For household scope, we lean on short lifetimes rather than active revocation checks.

## Operator tokens

Issued manually by the admin. Scope: `operator`, plus any specific namespaces. Lifetime: 1h like normal. Not auto-refreshed — operators re-auth each session.

The only way to get an operator token is via the CLI:

```
musubi-auth issue-operator --subject eric --ttl 1h
```

No web flow.

## Token passing to adapters

Each adapter stores its token differently:

- **MCP (stdio):** `MUSUBI_TOKEN` env var or `~/.musubi/token` file (0600).
- **MCP (HTTP):** OAuth 2.1 via MCP client's token handler.
- **LiveKit:** env var `MUSUBI_TOKEN` at worker startup.
- **OpenClaw:** `chrome.storage.local` (encrypted at rest by Chromium).

## Example: unauthorized capture

```
POST /v1/episodic
Authorization: Bearer <token with scope eric/claude-code/episodic:rw>

{"namespace": "eric/livekit-voice/episodic", "content": "..."}
```

→ 403

```json
{
  "error": {
    "code": "FORBIDDEN",
    "detail": "namespace 'eric/livekit-voice/episodic' not in token scope",
    "hint": "request a token with scope including this namespace"
  }
}
```

## Example: thought inbox check mismatch

```
POST /v1/thoughts/check
Authorization: Bearer <token with presence=claude-code, scope eric/claude-code/thought:r>

{"my_presence": "livekit-voice"}
```

→ 403. Tokens can only check their own presence's inbox.

## Auditing

Every auth decision is logged:

```json
{
  "ts": "...",
  "event": "auth.allow",
  "request_id": "...",
  "sub": "eric-claude-code",
  "endpoint": "POST /v1/episodic",
  "namespace": "eric/claude-code/episodic",
  "scope_used": "eric/claude-code/episodic:rw"
}
```

Denials have `event: auth.deny` + `reason: ...`. 30-day retention; operator-only read.

## Test Contract

**Module under test:** `musubi/auth/*`, `musubi-auth/`

1. `test_missing_bearer_returns_401`
2. `test_expired_token_returns_401`
3. `test_wrong_issuer_returns_401`
4. `test_scope_match_grants_access`
5. `test_scope_mismatch_returns_403_with_detail`
6. `test_operator_scope_required_for_admin_endpoints`
7. `test_thought_check_scope_is_presence_specific`
8. `test_blended_query_expands_and_checks_plane_scopes`
9. `test_pkce_flow_end_to_end` (integration)
10. `test_refresh_token_rotation_issues_new_refresh`
11. `test_revocation_invalidates_token_within_60s_cache`
12. `test_signing_key_rotation_dual_verify_period`
13. `test_every_auth_decision_emits_audit_line`
14. `test_operator_issued_only_via_cli`
