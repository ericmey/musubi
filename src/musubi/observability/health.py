"""Per-component health probes.

Per [[09-operations/observability]] § Inference metrics + Aoi's v0.1
review ask: ``/ops/status`` populates ``StatusResponse.components``
with one :class:`ComponentStatus` per dependency (TEI dense / sparse /
reranker, Ollama, vector-store, lifecycle-worker). Each probe is a
cheap GET against the dependency's ``/health`` endpoint; failure
modes (timeout, connect error, 4xx, 5xx) all surface as
``healthy=False`` with a one-line ``detail``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from musubi.api.responses import ComponentStatus

_PROBE_TIMEOUT_S = 1.5
"""How long to wait per probe. Status endpoints must stay fast — a
slow probe blocks the whole /ops/status response — so we cap each
sub-probe and let the timeout itself surface as `healthy=False`."""


def check_component_health(
    *,
    name: str,
    url: str,
    transport: httpx.BaseTransport | None = None,
    timeout: float = _PROBE_TIMEOUT_S,
) -> ComponentStatus:
    """Probe a downstream service. Returns a typed component-status row."""
    # Local import sidesteps the circular: api.app imports
    # observability for middleware install, observability.health needs
    # api.responses for the response model.
    from musubi.api.responses import ComponentStatus

    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        return ComponentStatus(name=name, healthy=False, detail=repr(exc))
    if resp.status_code >= 400:
        return ComponentStatus(
            name=name,
            healthy=False,
            detail=f"HTTP {resp.status_code} from {url}",
        )
    return ComponentStatus(name=name, healthy=True)


__all__ = ["check_component_health"]
