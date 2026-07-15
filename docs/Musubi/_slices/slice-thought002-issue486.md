---
title: "Slice: THOUGHT-002 production thought writes use configured embedder"
slice_id: slice-thought002-issue486
issue: 486
section: _slices
type: slice
status: in-progress
owner: shiori
phase: "Retrieval"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-07-15
reviewed: false
depends-on: []
blocks: []
---

# Slice: THOUGHT-002 production thought writes use configured embedder

## What
Replaces explicit `FakeEmbedder()` usage inside the `writes_thoughts.py` API router with the correct `Depends(get_thoughts_plane)` dependency.

## Why
Production routes `/v1/thoughts/send` and `/v1/thoughts/read` were manually instantiating `ThoughtsPlane(client=qdrant, embedder=FakeEmbedder())` instead of using the dependency injection framework, bypassing the production TEI embedder and inserting fake/zero vectors into Qdrant.

## Contract
1. A spy configured `ThoughtsPlane` proves `/send` calls that plane and its embedder.
2. The saved vector discriminates between two inputs under a deterministic nonzero test embedder; a zero/fake vector fails the test.
3. `/read` calls the configured plane and preserves missing-ID continuation.
4. Missing/unconfigured dependency fails loudly through the existing dependency contract.
5. Existing thought API and stream suites remain green.
