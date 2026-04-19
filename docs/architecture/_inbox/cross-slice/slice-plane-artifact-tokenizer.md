---
title: "Cross-slice: Tokenizer wiring for artifact chunkers"
section: _inbox/cross-slice
tags: [cross-slice, type/ticket, status/blocked]
updated: 2026-04-19
---
# Tokenizer wiring for artifact chunkers

**From:** `slice-plane-artifact`
**To:** `slice-llm-client` (or whichever slice owns tokenizer loading)

The `TokenSlidingChunker` and `MarkdownHeadingChunker` in `musubi/planes/artifact/chunking.py` currently use a naive `text.split()` (whitespace/words) implementation. They need to be wired up to the real tokenizer (e.g. BGE-M3 tokenizer) to properly enforce the 512-token window and 128-token overlap, and to handle oversize markdown sections.

This ticket tracks replacing the naive implementation with the real tokenizer.
