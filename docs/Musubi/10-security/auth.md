---
title: Auth
section: 10-security
tags: [auth, jwt, scopes, section/security, security, status/complete, tokens, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[10-security/index]]"
reviewed: false
implements: ["src/musubi/auth/", "tests/auth/"]
---
# Auth

Authentication and authorization for Musubi Core. Every protected call carries a
JWT bearer token. Core validates the token itself and then checks its scopes
against the namespace the call touches. The code is `src/musubi/auth/tokens.py`
(validation), `src/musubi/auth/scopes.py` (scope checks and audit events) and
`src/musubi/auth/middleware.py` (the request helper the routes call).

Core does not issue tokens. There is no login flow, no token endpoint, no refresh
token and no per-token revocation. Whoever holds the signing key (or runs the
identity provider) mints tokens; Core only verifies them.

## Model

```
┌──────────────┐   Authorization: Bearer <jwt>   ┌───────────────────────────┐
│ Agent / SDK  │ ──────────────────────────────▶ │ Musubi Core               │
│ / plugin     │   (optionally through an        │ 1. validate signature +   │
└──────────────┘    operator-provided TLS        │    claims                 │
                    reverse proxy)               │ 2. check scope against    │
                                                 │    the requested namespace│
                                                 └───────────────────────────┘
```

1. The client sends `Authorization: Bearer <jwt>`.
2. Core verifies the signature and the claims (below). Failure is a 401.
3. The route checks the token's scopes against the namespace (or the `operator`
   requirement). Failure is a 403.

## Signing: HS256 or RS256

Core accepts exactly two algorithms, chosen by the token's `alg` header
(`tokens.py:19`, `tokens.py:109-122`). Anything else is rejected.

- **HS256 (default).** The token is signed with the shared secret in the
  `JWT_SIGNING_KEY` environment variable (`settings.py:277`). Anyone who holds
  that key can mint tokens, so treat it like a root credential.
- **RS256 via the issuer's JWKS.** Core fetches
  `<OAUTH_AUTHORITY>/.well-known/jwks.json` (`tokens.py:252-253`) and picks the key
  whose `kid` matches the token header. A token without a `kid` header is rejected
  (`tokens.py:130-132`). Core does not cache the JWKS: it fetches it on each
  validation, with a 5-second timeout. Core does not publish a JWKS of its own.

Both settings are required at startup. `OAUTH_AUTHORITY` is also the expected
issuer for HS256 tokens.

## Token claims

```json
{
  "iss": "https://auth.example.test",
  "aud": "musubi",
  "sub": "alex/claude-code",
  "presence": "alex/claude-code",
  "scope": "alex/claude-code:r alex/claude-code/*:rw alex/shared/curated:r",
  "iat": 1790000000,
  "exp": 1790003600
}
```

| claim | rule | source |
|---|---|---|
| `iss` | required; must equal `OAUTH_AUTHORITY` without a trailing `/` | `tokens.py:89`, `tokens.py:248-249` |
| `aud` | required; the string `"musubi"` | `tokens.py:18`, `tokens.py:175` |
| `presence` | required; a concrete `tenant/name` identity: exactly two non-empty segments, no `*` | `tokens.py:217-223` |
| `sub` | required; must equal `presence` | `tokens.py:225-226` |
| `scope` | required; a space-separated string or a JSON list of strings | `tokens.py:202-207` |
| `exp`, `nbf`, `iat` | checked if present (PyJWT). Core does not require `exp`, but a token without it never expires, so always set one | `tokens.py:84-92` |
| `jti` | optional; must be a string if present. Core does not check it against anything | `tokens.py:179-180` |

One more cross-check: every namespace scope that names a concrete tenant must name
the **same tenant as `presence`**. A token for `alex/claude-code` that carries
`sam/discord/episodic:r` is not a narrower token, it is an invalid one: the whole
token is rejected with a 401 (`tokens.py:228-232`). Wildcard-tenant scopes
(`*/...`, `**`) and `operator` are exempt from this check (`tokens.py:236-245`).

## Scope syntax

```
<namespace-glob>:<access>
```

Access is `r` (read), `w` (write) or `rw` (both). A scope without a `:` or with any
other access string never matches a namespace (`scopes.py:211-217`).

The namespace glob is matched segment by segment (`scopes.py:220-232`):

- A literal segment must be equal.
- `*` matches exactly one segment.
- **The glob must have the same number of segments as the namespace.** `alex/*:rw`
  matches the 2-segment `alex/voice` and nothing else; it does not cover
  `alex/voice/episodic`. Use `alex/*/*:rw` for every plane of every Alex presence.
- `**` on its own matches every namespace, **for reads only**. `**:rw` and `**:w`
  never grant write access (`scopes.py:205-206`). Use explicit segment wildcards for
  scoped writes.

Examples (namespaces are `tenant/presence/plane`; see [[03-system-design/namespaces]]):

| scope | grants |
|---|---|
| `alex/voice/episodic:rw` | read and write one namespace |
| `alex/*/episodic:r` | read Alex's episodic plane on every presence |
| `alex/voice/*:rw` | read and write every plane of `alex/voice` |
| `alex/voice:r` | 2-segment reads: cross-plane retrieve and the thoughts stream for `alex/voice` |
| `alex/shared/curated:r` | read Alex's shared curated knowledge |
| `*/*/episodic:r` | read every agent's episodic plane (a cross-tenant survey scope) |
| `**:r` | read everything |

Some endpoints take a 2-segment namespace, so a presence token typically carries
both `<tenant>/<presence>:r` and `<tenant>/<presence>/*:rw`. The per-endpoint table
is in [[07-interfaces/canonical-api]] (Scope by endpoint).

### The `operator` scope

The literal scope `operator` is required by the operator endpoints
(`scopes.py:180-196`), by hard delete (`DELETE /v1/episodic/{id}?hard=true`) and by
artifact purge. Operator tokens also get a 10x multiplier on the per-token write
rate limits (`src/musubi/api/rate_limit.py`).

An operator token is still an ordinary token: it needs a concrete `presence`, a
matching `sub`, and the claims above. There is no special command or flow for it.
Mint it the same way as an agent token, add `operator` to its scope, give it a
short `exp`, and do not hand it to agents.

### Thoughts

There is no separate `thoughts:*` keyword scope. Every thoughts endpoint checks the
standard namespace form:

- `POST /v1/thoughts/send` and `POST /v1/thoughts/read` need `w` on the 3-segment
  `<tenant>/<presence>/thought` namespace (marking read mutates state).
- `POST /v1/thoughts/check` and `POST /v1/thoughts/history` need `r` on that
  namespace.
- `GET /v1/thoughts/stream` needs `r` on the 2-segment `<tenant>/<presence>`.

## Validation pipeline

On each protected request Core:

1. Reads `Authorization: Bearer <jwt>`. Missing or not a bearer: 401
   `missing bearer token` (`middleware.py:59-67`).
2. Reads the unverified header and rejects any `alg` other than HS256 or RS256.
3. Resolves the key: `JWT_SIGNING_KEY` for HS256, the matching JWKS key for RS256.
4. Verifies the signature and `iss`, `aud`, `exp`, `nbf` (PyJWT). An expired token
   is a 401 `token expired`; any other failure is a 401 with PyJWT's message.
5. Requires `sub`, `iss`, `aud`, `presence` and a well-formed `scope`; requires a
   concrete `presence`, `sub == presence`, and that every concrete-tenant scope
   names the presence's tenant. Failure: 401.
6. Checks the route's requirement: a namespace with `r` or `w`, or `operator`.
   Failure: 403 (`scopes.py:105-141`).

A bearer token is validated even on public routes. If a request **presents** a
bearer that does not validate, Core answers 401 instead of serving the request
anonymously (`src/musubi/api/presented_bearer.py`). A request with no
`Authorization` header still reaches public routes.

### Network-protected read-only ops endpoints

`GET /v1/ops/health`, `GET /v1/ops/status` and `GET /v1/ops/metrics` do not require
a token, so that deployment probes and a Prometheus scraper can reach them. They
are not safe to expose publicly: restrict them at the network layer (a host
firewall, a private Compose network, or the reverse proxy). Mutating and debug ops
endpoints still require `operator`. The decision and its accepted disclosure are
recorded in [[13-decisions/0038-network-protect-read-only-ops-endpoints]].

## Scope checks on retrieval

A retrieval can touch several namespaces. `POST /v1/retrieve` resolves the request
into concrete `(namespace, plane)` targets and runs every target through one
read-only enforcement seam, `enforce_namespace_policy` (`scopes.py:40-102`):

- **2-segment or multi-plane requests** are expanded to one namespace per plane,
  and the token needs read scope on **each** of them.
- **Wildcard namespaces** (`alex/*/episodic`) are expanded against the stored data
  first; each expanded target must then pass the scope check.
- For both of these, one unreadable target rejects the whole request with a 403.
- **No namespace at all** recalls across the caller's own tenant: Core enumerates
  the stored namespaces for the presence's tenant and keeps only the ones the token
  can read. In this mode an unreadable namespace is silently dropped (and not
  logged as a denial) rather than failing the request
  (`src/musubi/api/routers/retrieve.py`).

After authorization, Core removes any namespace on the configured exclusion lists
(the `default_excluded_namespaces` and `per_agent_excluded_namespaces` settings,
keyed by `sub` or `presence`). An explicitly requested namespace that is excluded
returns an empty result, not a 403.

## Token lifetime, rotation and revocation

- **Lifetime** is whatever the issuer puts in `exp`. Core imposes no maximum. Keep
  agent tokens reasonably short and operator tokens short.
- **No refresh tokens.** A client gets a new token the same way it got the first one.
- **No per-token revocation.** Core keeps no revocation list and ignores `jti`. To
  invalidate a leaked HS256 token, rotate `JWT_SIGNING_KEY` and restart Core; every
  token signed with the old key stops validating at once, so re-mint the ones you
  still need. With RS256, revoke at the identity provider by removing the key from
  its JWKS (Core re-reads the JWKS on every validation).
- **Overlapping keys** work only under RS256: while the JWKS lists both the old and
  the new `kid`, tokens signed by either validate. HS256 has a single key.

## Token passing to clients

Clients pass the token in the `MUSUBI_TOKEN` environment variable:

- the in-repo MCP adapter reads `MUSUBI_TOKEN` and `MUSUBI_API_URL`
  (`src/musubi/adapters/mcp/server.py`);
- the `musubi` CLI and `musubi-context` read `MUSUBI_TOKEN` or `--token`.

External plugins document their own token storage; see each plugin's repository
(listed in the [user guide](../../guide/connect.md)).

## Example: unauthorized capture

```
POST /v1/episodic
Authorization: Bearer <token for alex/claude-code with scope alex/claude-code/episodic:rw>

{"namespace": "alex/voice/episodic", "content": "..."}
```

→ 403

```json
{
  "error": {
    "code": "FORBIDDEN",
    "detail": "namespace 'alex/voice/episodic' not in token scope for 'w' access",
    "hint": ""
  }
}
```

The detail string comes from `scopes.py:131`; the envelope is
`src/musubi/api/errors.py`.

## Example: thought inbox namespace mismatch

```
POST /v1/thoughts/check
Authorization: Bearer <token for alex/claude-code with scope alex/claude-code/thought:r>

{"namespace": "alex/voice/thought", "presence": "voice"}
```

→ 403. The token grants `alex/claude-code/thought:r`; the request names
`alex/voice/thought`. The matcher compares the requested namespace against the
token's scopes verbatim. It does not infer anything from the token's own presence.

## Auditing

Every scope decision emits one structured log record on the `musubi.auth.scopes`
logger at INFO, with message `auth.allow` or `auth.deny` (`scopes.py:239-257`):

```json
{
  "ts": "2026-10-01T10:21:34.512Z",
  "level": "info",
  "service": "musubi.auth.scopes",
  "msg": "auth.allow",
  "request_id": "...",
  "event": "auth.allow",
  "sub": "alex/claude-code",
  "namespace": "alex/claude-code/episodic",
  "access": "w",
  "scope_used": "alex/claude-code/episodic:rw"
}
```

Denials carry `"event": "auth.deny"`, `"scope_used": null` and a `reason`. Token
validation failures (the 401 cases) are not audit events. Records go to the
process's standard log stream; retention and access control are whatever the
operator's log pipeline provides. See [[10-security/audit]].

## Test Contract

**Module under test:** `src/musubi/auth/*` (tests in `tests/auth/test_auth.py`)

1. `test_missing_bearer_returns_401`
2. `test_expired_token_returns_401`
3. `test_wrong_issuer_returns_401`
4. `test_scope_match_grants_access`
5. `test_scope_mismatch_returns_403_with_detail`
6. `test_operator_scope_required_for_admin_endpoints`
7. `test_blended_query_expands_and_checks_plane_scopes`
8. `test_recursive_scope_grants_read_without_write`
9. `test_signing_key_rotation_dual_verify_period` (RS256 JWKS with two keys)
10. `test_every_auth_decision_emits_audit_line`

PKCE, refresh-token, revocation-cache and operator-issuing-CLI tests from the earlier
OAuth design remain in the test file as skipped placeholders; those features are not
implemented.
