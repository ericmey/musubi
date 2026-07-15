---
title: "Slice: THOUGHT-001 check/history presence filtering"
slice_id: slice-thought001-issue477
issue: 477
section: _slices
type: slice
status: in-progress
owner: shiori
phase: "Retrieval"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-07-14
reviewed: false
depends-on: []
blocks: []
---

# Slice: THOUGHT-001 check/history presence filtering

## What
Fixes `/v1/thoughts/check` and `/v1/thoughts/history` to enforce presence filtering according to their respective semantics.

## Why
Currently these routes only filter by `namespace` and ignore the required `presence` body argument, allowing callers to view unrelated thoughts.

## Contract
1. `/check` returns thoughts addressed to the requested presence or `"all"`, excluding self-sends and read-by-me rows.
2. `/history` returns thoughts addressed to the requested presence, `"all"`, or sent by the requested presence, while excluding unrelated rows.
3. Namespace authorization remains enforced.
4. Existing stream and write behaviour remain unchanged.
