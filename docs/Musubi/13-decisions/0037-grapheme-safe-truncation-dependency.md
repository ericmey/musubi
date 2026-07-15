---
title: "0037: Grapheme-Safe Truncation Dependency"
section: 13-decisions
tags: [architecture, python, retrieval, dependencies, type/adr, status/accepted]
type: adr
updated: 2026-07-15
status: accepted
---

# 0037: Grapheme-Safe Truncation Dependency

## Context

Musubi's retrieval pipelines (`/v1/context`, `/v1/retrieve`) enforce character-count bounds (`max_chars`) to ensure downstream LLMs and APIs operate within strict latency and token budgets. Historically, this truncation was executed natively via Python string slicing (`text[:max_chars]`).

However, Python string slicing operates at the level of Unicode codepoints, not visual characters (grapheme clusters). This causes severe presentation issues when a slice natively bisects complex glyphs. Examples include:
- **ZWJ Sequences:** A family emoji 👨‍👩‍👧‍👦 consists of multiple individual codepoints joined by Zero-Width Joiners. Bisecting this sequence outputs fractured independent emojis or dangling joiners.
- **Combined Diacritics:** A base letter `e` plus an acute accent `´` are two codepoints. Bisecting leaves the base letter and truncates the accent.
- **Regional Indicators:** Country flags consisting of dual indicator symbols (e.g., 🇺🇸) can be severed, leaving a dangling half-symbol.

These bisections yield mathematically valid Unicode but produce semantically broken, fragmented glyphs on the client UI.

## Decision

We will replace naive Python string slicing with grapheme-cluster-aware truncation across all retrieval projection lanes (`fast`, `recent`, `ranked`, `context`).

We will introduce the well-maintained `regex` library (and `types-regex` for typing) as an explicit runtime dependency to natively evaluate the PCRE `\X` grapheme boundary match.

### Why `regex`?
- Standard library `re` does not support the `\X` extended grapheme cluster sequence natively.
- Hand-rolling a UTF-8/codepoint loop verifying combining ranges, ZWJs, and regional indicators is error-prone, fundamentally brittle against future Unicode standard updates, and functionally inferior to the C-backed optimizations inside `regex`.
- `regex` is a mature, widely accepted standard in the Python ecosystem designed exactly as a drop-in superset for `re`.

### Truncation Policy
The `max_chars` parameter conceptually defines the payload size limit, which we continue to treat strictly as a **codepoint budget** for wire format parity.
The algorithm will step forward by grapheme clusters. It will select the final completely-rendered grapheme cluster whose appended string length does not exceed `max_chars` (or `max_chars - 3` if padding with `...`).
The truncation helper intrinsically preserves internal and trailing whitespace entirely unaltered; any whitespace normalization (e.g., in the Context Pack pipeline) must be performed by the caller before invoking the truncation helper.

## Consequences
- **Positive:** Terminal users and downstream LLM contexts will never receive fractured visual characters or corrupted emoji modifiers.
- **Positive:** We avoid adopting a heavy bespoke library (like `grapheme` or `uniseg`) by using a library (`regex`) that solves multiple potential future parsing needs.
- **Negative:** Addition of a compiled third-party C-extension dependency (`regex`) to the core environment, adding slight overhead to the environment installation.
