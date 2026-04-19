"""Canonical HTTP API (v0.1) — read surface.

The single FastAPI app over which adapters (MCP, LiveKit, OpenClaw,
SDKs) talk to Musubi Core. Per ADR-0011 + ADR-0013, pydantic models in
``src/musubi/types/`` are the source of truth; FastAPI generates
OpenAPI 3.1 at runtime from route signatures.

This module ships the **read surface**: GET endpoints + POST reads
(retrieve, thoughts/check, thoughts/history) + auth middleware + error
taxonomy + pagination + health probes. The write surface is
``slice-api-v0-write`` — POST captures, PATCH updates, lifecycle
transitions, rate-limit middleware applied to mutations, idempotency
key cache.

See [[07-interfaces/canonical-api]] + [[07-interfaces/contract-tests]].
"""

from musubi.api.app import create_app

__all__ = ["create_app"]
