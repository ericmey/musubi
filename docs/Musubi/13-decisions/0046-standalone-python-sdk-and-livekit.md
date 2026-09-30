---
title: "ADR 0046: Standalone Python SDK and LiveKit adapter"
section: 13-decisions
type: adr
status: accepted
date: 2026-09-30
updated: 2026-09-30
deciders: [Eric]
tags: [architecture, sdk, adapters, livekit, type/adr, status/accepted]
supersedes: "ADR 0015 and ADR 0022 on Python SDK and LiveKit source location and distribution"
superseded-by: ""
---

# ADR 0046: Standalone Python SDK and LiveKit adapter

- **Status:** Accepted
- **Date:** 2026-09-30
- **Decider:** Eric

## Context

ADR 0015 and ADR 0022 put the Python SDK and LiveKit adapter in the Musubi
server repository. A LiveKit worker therefore pulls the server package and its
dependencies to use the adapter. Other Musubi integrations have since moved to
sibling repositories. Eric directed the SDK and LiveKit adapter to become
separate packages during the 2026-09-30 repository cleanup.

## Decision

- `sourceblender/musubi-sdk` owns the sync and async HTTP clients, errors,
  retries, results, tracing helpers and test fakes. It publishes `musubi-sdk`,
  whose import root is `musubi_sdk` and whose runtime dependency is `httpx`.
- `sourceblender/musubi-livekit` owns the session callback adapter and its tests.
  It publishes `musubi-livekit`, imports `musubi_sdk`, and has no runtime
  dependency on the Musubi server package. A LiveKit worker owns its own event
  subscriptions, credentials and user-facing behavior; installing the adapter
  does not install a worker.
- Core keeps forwarding modules at `musubi.adapters.livekit` for existing Python
  import paths. They import the external plugin when it is installed and give
  an explicit install error when it is absent. New code uses `musubi_livekit`.
  During migration the plugin catches both the old core SDK error hierarchy and
  the standalone SDK hierarchy, because existing callers may pass either client.
- The canonical HTTP API and its server remain in `sourceblender/musubi`.
  Integrations continue to use that API, never storage internals.

## Migration and proof

Publish and verify the standalone SDK before publishing a plugin wheel that
depends on it. Verify the plugin in a clean environment without the server,
then run its disposable-stack integration tests. Only after both packages are
available should core replace its old implementation with forwarding modules.
The old and new import paths must resolve to the same classes when the plugin
is installed. Review installed consumers before switching any production
worker; a passing package test is not a consumer migration.

## Consequences

The SDK, plugin and core now need coordinated compatibility tests and separate
release timing. In exchange, a LiveKit worker installs only its client and
adapter dependencies, and protocol-specific code no longer ships in the
server. This decision supersedes ADR 0015 and ADR 0022 only for the Python SDK
and LiveKit source location and packaging; their other decisions remain history.
