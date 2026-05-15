"""Factory for production-grade QdrantClient construction.

The qdrant-client library raises ``UserWarning: Api key is used with an
insecure connection`` whenever an API key is supplied over plain HTTP.
On musubi's compose bridge that warning is structurally noisy:

- Qdrant lives on the internal ``musubi_default`` Docker network, never
  reachable from outside the host.
- mTLS for intra-compose traffic is tracked separately (see
  ``MUSUBI_ALLOW_PLAINTEXT`` env flag + the future hardening slice).
- The warning fires on every container start — twice for the lifecycle
  worker (during signal handler reconnect), and once for core.

Rather than ``warnings.filterwarnings("ignore", ...)`` globally (which
would hide *all* qdrant warnings, including ones we'd want to see), this
factory wraps the construction in a tightly scoped ``catch_warnings``
block and silences only the one message we know is intentional. Any
future qdrant_client warning surfaces unchanged.
"""

from __future__ import annotations

import re
import warnings

from qdrant_client import QdrantClient

# `warnings.filterwarnings` treats `message` as a regex matched from
# the start of the warning text. Escape the literal — the trailing `.`
# is a literal period in the upstream message, not a regex wildcard —
# and add `\Z` so we only match this exact warning.
_INSECURE_API_KEY_WARNING = re.escape("Api key is used with an insecure connection.") + r"\Z"


def build_qdrant_client(
    *,
    host: str,
    port: int,
    api_key: str,
    https: bool = True,
) -> QdrantClient:
    """Construct a ``QdrantClient`` with the documented insecure-API-key
    warning suppressed when ``https`` is False.

    All non-``https`` callsites in production accept the trade-off — the
    network is internal-only and the API key is rotated via vault. The
    factory documents that choice in one place instead of scattering
    ``warnings.catch_warnings`` blocks across modules.
    """
    with warnings.catch_warnings():
        # The `\Z` anchor on the message pattern is the precision
        # guarantee here — we suppress only the exact documented
        # warning. The `module` filter was tempting as belt-and-braces
        # but excludes warnings whose stacklevel resolves outside
        # `qdrant_client` (test stubs, future code paths) and led to
        # leakage in tests. Message-anchor is enough.
        warnings.filterwarnings(
            "ignore",
            message=_INSECURE_API_KEY_WARNING,
            category=UserWarning,
        )
        return QdrantClient(host=host, port=port, api_key=api_key, https=https)


__all__ = ["build_qdrant_client"]
