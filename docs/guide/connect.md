# Connect an agent

Every agent reaches Musubi with its own bearer token and its own identity.
Give each agent host (a Claude Code seat, a voice worker, …) a separate token.

## Identity and tokens

A token is a JWT. With the default single-host setup it's signed HS256 with
`JWT_SIGNING_KEY`. (Musubi also accepts RS256 tokens from an OAuth authority
that publishes a JWKS.) The server rejects a token unless all of these hold:

| claim | rule |
|---|---|
| `iss` | equals the server's `OAUTH_AUTHORITY`, without a trailing `/` |
| `aud` | `"musubi"` |
| `presence` | a concrete `tenant/name` identity, such as `alex/voice`: exactly two parts, no `*` |
| `sub` | equals `presence` |
| `scope` | space-separated scopes; every namespace scope names the same tenant as `presence` |
| `exp` | if present, in the future. The server doesn't require it, but a token without `exp` never expires, so always set one |

Scopes grant access per namespace. A namespace is `tenant/name/plane`, for
example `alex/voice/episodic`:

- `alex/voice/episodic:rw` reads and writes that namespace.
- `alex/voice/episodic:r` only reads it; `:w` only writes.
- `*` matches exactly one segment, and a scope must have as many segments as
  the namespace: `alex/voice/*:rw` covers every plane of `alex/voice`, while
  the 2-segment `alex/voice:r` is needed for cross-plane retrieve and the
  thoughts stream.
- `operator` is for administrative calls only; don't give it to agents.

Musubi has no command or endpoint that issues tokens; you mint them yourself,
as below. An operator token is minted the same way: add `operator` to its
`scope`, keep a concrete `presence` (with `sub` equal to it), and give it a
short `exp`.

Minting one with PyJWT (run this where the signing key is available, and
hand the resulting token to the agent through its secret store):

```python
import os
from datetime import UTC, datetime, timedelta

import jwt

now = datetime.now(UTC)
presence = "alex/voice"
token = jwt.encode(
    {
        "iss": os.environ["OAUTH_AUTHORITY"].rstrip("/"),
        "aud": "musubi",
        "sub": presence,
        "presence": presence,
        "scope": f"{presence}:r {presence}/*:rw",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=30)).timestamp()),
    },
    os.environ["JWT_SIGNING_KEY"],
    algorithm="HS256",
)
```

The full scope grammar, including cross-namespace reads, is in
[the auth spec](../Musubi/10-security/auth.md); the namespace model is in
[Namespaces](../Musubi/03-system-design/namespaces.md).

## Plugins

Each plugin lives in its own repository and has its own install steps.
Follow that repository's README, since it tracks the plugin's current
release.

| agent host | plugin |
|---|---|
| Claude Code | [musubi-claude](https://github.com/sourceblender/musubi-claude) |
| Codex | [musubi-codex](https://github.com/sourceblender/musubi-codex) |
| Grok Build | [musubi-grok](https://github.com/sourceblender/musubi-grok) |
| Hermes Agent | [musubi-hermes](https://github.com/sourceblender/musubi-hermes) |
| OpenClaw | [musubi-openclaw](https://github.com/sourceblender/musubi-openclaw) (npm package `openclaw-musubi`) |
| OpenCode | [musubi-opencode](https://github.com/sourceblender/musubi-opencode) |
| LiveKit voice workers | [musubi-livekit](https://github.com/sourceblender/musubi-livekit) |

Most of the plugins share one runtime,
[musubi-harness](https://github.com/sourceblender/musubi-harness): durable
local outbox, verified writes, and identity from configuration. Each plugin
installs the version it needs.

## Your own code

For anything without a plugin, use the Python client
[`musubi-sdk`](https://pypi.org/project/musubi-sdk/) (`pip install musubi-sdk`).
It depends only on `httpx`; see [Use it](use.md) for an example. Any other
language can call the HTTP API directly; see
[the canonical API](../Musubi/07-interfaces/canonical-api.md).
