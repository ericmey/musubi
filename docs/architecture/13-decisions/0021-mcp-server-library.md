---
title: "ADR 0021: Use Anthropic's `mcp` package"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-19
deciders: [Eric, Nyla]
tags: [section/decisions, status/accepted, type/adr, mcp, dependencies]
updated: 2026-04-19
up: "[[13-decisions/index]]"
reviewed: true
---

# ADR 0021: Use Anthropic's `mcp` package

**Status:** accepted
**Date:** 2026-04-19
**Deciders:** Eric, Nyla

## Context
`slice-adapter-mcp` exposes the Musubi client capabilities via the Model Context Protocol. We need a robust implementation of the MCP server-side protocol that handles both local (`stdio`) and remote (`streamable-http` with SSE) transports.

## Decision
Add the official `mcp` Python package (>= 1.2.0) to `pyproject.toml` dependencies.

## Consequences
- Single new top-level dependency (and its transitive graph).
- Eliminates writing a custom JSON-RPC / SSE framing implementation.
- Gives us out-of-the-box compatibility with Claude Code, Cursor, and any other MCP-compliant client.
- Exposes `mcp.server.FastMCP` which drastically simplifies tool definitions.

## Alternatives considered
- **Writing our own JSON-RPC over stdio + SSE** — Significant wheel reinvention for a standardized protocol that already has an official reference implementation.
