---
title: "ADR 0020: Use `python-multipart` for FastAPI `multipart/form-data` uploads"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-19
deciders: [Eric, vscode-cc-sonnet47]
tags: [section/decisions, status/accepted, type/adr, api, dependencies]
updated: 2026-04-19
up: "[[13-decisions/index]]"
reviewed: false
---

# ADR 0020: Use `python-multipart` for FastAPI `multipart/form-data` uploads

**Status:** accepted
**Date:** 2026-04-19
**Deciders:** Eric, vscode-cc-sonnet47

## Context

`slice-api-v0-write` (PR #78) ships `POST /v1/artifacts`, the canonical artifact-upload endpoint per [[07-interfaces/canonical-api]] § Content types. The endpoint accepts `multipart/form-data` (a binary file part + metadata form fields) per the same spec — the only Musubi route that does, today.

FastAPI's `File()` / `Form()` / `UploadFile` parameter helpers route to Starlette's multipart parser, which in turn requires the `python-multipart` package at runtime. Without it, route registration succeeds but the first request raises `RuntimeError: Form data requires "python-multipart" to be installed.` — a deferred failure rather than an import-time one. The dep was added quietly in PR #78 (`pyproject.toml` line for `python-multipart>=0.0.9`) without an accompanying ADR; the operator's review note flagged this as soft drift from the "new top-level deps require an ADR" rule. This ADR closes that gap retroactively, mirroring ADR-0017 (`watchdog`) and ADR-0018 (`ruamel.yaml`).

## Decision

Use `python-multipart >= 0.0.9` as the multipart-form parser for FastAPI's `File`/`Form`/`UploadFile` surface. Pin the floor at the version FastAPI's docs currently recommend; let the upper bound float within the major version.

## Consequences

- Adds one top-level dep. Pure Python, ~30 KB installed, no transitive bloat.
- The dep is strictly contained within `src/musubi/api/routers/writes_artifact.py` — no other module imports it. Removing the artifact-upload route would let the dep go too.
- FastAPI's runtime check (`ensure_multipart_is_installed`) gates the route's first invocation, not its registration. Tests for the artifact upload endpoint (`tests/api/test_api_v0_write.py::test_multipart_upload_for_artifacts`) require the dep to be installed — that test serves as the integration check that the dep is present in the dev extras.
- No security surface change beyond what FastAPI's own multipart handling already exposes; `python-multipart` is the maintained reference parser FastAPI recommends.

## Alternatives considered

- **Hand-roll a multipart parser inside the route handler.** Wheel-reinvention; introduces maintenance overhead for an RFC 7578 implementation that FastAPI already integrates with.
- **Switch the artifact upload endpoint to `application/json` + base64-encoded body.** Possible but adds ~33 % size overhead for binary files and breaks every off-the-shelf client (curl, Postman, the future SDK's file-upload sugar). Rejected — the spec explicitly chose `multipart/form-data` for this endpoint.
- **Stream the raw body via `Request.stream()` and parse manually.** Same wheel-reinvention concern; gains nothing over the maintained parser.

## References

- FastAPI docs on file uploads: https://fastapi.tiangolo.com/tutorial/request-files/
- python-multipart on PyPI: https://pypi.org/project/python-multipart/
- The shipping change: PR #78 (`feat(api): slice-api-v0-write`), `pyproject.toml` diff.
- Sibling ADRs that document tiny-dep additions in the same shape: [[13-decisions/0017-watchdog-for-vault-fs-watcher]], [[13-decisions/0018-ruamel-yaml-for-format-preserving-frontmatter]].
