---
title: "Cross-slice: Tokenizer wiring for artifact chunkers"
section: _inbox/cross-slice
type: cross-slice
status: resolved
tags: [section/inbox-cross-slice, type/cross-slice, status/resolved]
updated: 2026-04-20
---

## Resolution

Resolved by the PR closing this branch (chore/artifact-tokenizer) —
`src/musubi/planes/artifact/chunking.py` now uses the BGE-M3 tokenizer
via the minimal `tokenizers` HF dependency (not the full `transformers`
package). `TokenSlidingChunker` windows at exactly 512 tokens with
128-token overlap; `MarkdownHeadingChunker` delegates oversize sections
to the token splitter, preserving heading-path metadata and marking
split chunks with `split_from_oversize_section=True`. Small sections
stay single-chunk without paying tokenizer-load cost (lazy-load gated
on `_likely_within_window` heuristic + injection in tests).

Codex drove the bulk of the implementation (~268 lines in
`chunking.py`, new `tokenizers` dep in `pyproject.toml` + `uv.lock`)
before his credits ran out mid-session. Operator picked up and
finished: 8 new tokenizer tests in `tests/planes/test_artifact.py`
covering exact window + overlap math, oversize markdown splits,
heading-path preservation, sentence-boundary preference, empty text,
invalid-overlap validation; fixed ruff `RUF001` on CJK sentence-end
regex (noqa + inline comment); added `tokenizers.*` to mypy
ignore-missing-imports override.

Original ticket preserved below for audit.

---

# Tokenizer wiring for artifact chunkers

**From:** `slice-plane-artifact`
**To:** `slice-llm-client` (or whichever slice owns tokenizer loading)

The `TokenSlidingChunker` and `MarkdownHeadingChunker` in `musubi/planes/artifact/chunking.py` currently use a naive `text.split()` (whitespace/words) implementation. They need to be wired up to the real tokenizer (e.g. BGE-M3 tokenizer) to properly enforce the 512-token window and 128-token overlap, and to handle oversize markdown sections.

This ticket tracks replacing the naive implementation with the real tokenizer.
