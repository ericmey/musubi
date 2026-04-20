"""Adapters layer — protocol-specific shims over :mod:`musubi.sdk`.

Per ADR-0015 / ADR-0016, every adapter (LiveKit, MCP, OpenClaw) lives
in-monorepo as a sub-package. Adapters import the SDK + types only;
they never reach into ``api/``, ``planes/``, ``retrieve/`` etc.
"""
