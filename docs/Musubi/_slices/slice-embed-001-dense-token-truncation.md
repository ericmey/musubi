---
title: "Slice: EMBED-001 dense truncation delegated to TEI's tokenizer"
slice_id: slice-embed-001-dense-token-truncation
issue: 734
section: _slices
type: slice
status: done
owner: claude-code-opus5
phase: "Retrieval"
tags: [section/slices, status/done, type/slice]
updated: 2026-09-08
reviewed: false
depends-on: []
blocks: []
---

# Slice: EMBED-001 dense truncation delegated to TEI's tokenizer

> The dense embedding path guarded oversize input with a **character** limit
> standing in for BGE-M3's real **8192-token** limit. Token-dense markdown
> reaches ~2.1 chars/token, so a document could satisfy the character guard
> and still be rejected by TEI with HTTP 413 — which **dropped** the caller's
> write instead of truncating it. Fix: send `truncate=true` and let the
> deployed TEI apply the model's own tokenizer and runtime
> `max_input_length`.

**Phase:** Retrieval · **Status:** `done` (on merge) · **Owner:** `claude-code-opus5`

## Specs to implement

- [[_slices/slice-embed-001-dense-token-truncation|the locked contract for EMBED-001, this slice itself]] — combined contract + implementation; the Test Contract below is the locked spec.

## Impact this closes

- **23 vault reflections unembedded.** `vault-reconcile` reported
  `scanned=140 upserted=117 unchanged=0 errored=23` every boot — a
  consecutive daily run, `2026-08-17` .. `2026-09-08`. Unreachable by
  semantic recall. The line reporting it is `level=info` and says
  "complete".
- **Live episodic writes dropped.** 21 uncaught `EmbeddingError` on
  `POST /v1/episodic` in one 5-minute window on `musubi-core-1`.

## Owned paths

- `src/musubi/embedding/tei.py` — `TEIDenseClient.embed_dense` payload only
- `tests/test_embedding.py` — section 13
- `docs/Musubi/_slices/slice-embed-001-dense-token-truncation.md`

## Forbidden paths

- `src/musubi/embedding/chunked.py` — `ChunkedEmbedder` behaviour unchanged
- `src/musubi/planes/artifact/chunking.py` — SPLADE chunker untouched
- `src/musubi/api/**` — canonical API is frozen per version

## Why no character limit fixes this

Measured on real content:

| sample | chars | tokens | chars/token |
|---|---|---|---|
| failing 2026-09-08 reflection (Yua) | 17,618 | 8,403 | 2.10 |
| canary markdown, post-clip | 32,000 | 10,668 | 3.00 |

A safe character limit is ~17,200 chars; live vault files reach 23,900. Any
safe value truncates real content by up to ~28%. **No character limit is
both safe and non-destructive** — the proxy is unfixable at any value, which
is why the fix delegates rather than retunes.

## Test Contract

- `test_dense_embed_request_sets_truncate_true`
- `test_dense_embed_returns_vectors_for_token_dense_oversize_input`
- `test_dense_embed_still_clips_to_max_input_chars`
- `test_sparse_embed_request_does_not_set_truncate`
- `test_reranker_request_does_not_set_truncate`

## Controls (4 healthy controls)

Only the first bullet is red before the fix. The other four pin behaviour
that must **not** change, and all four passed on the red commit (49e348c) —
so they are controls, not incidental passes.

## Out of scope (declared)

- **Mean-pooling over token windows.** `chunked.py:20` prescribes it, and it
  is strictly better for recall on long documents. Deferred deliberately: it
  changes vector semantics and distribution, so it needs retrieval-quality
  evaluation plus a re-embed plan for existing vectors. Follow-up, agreed
  with Yua 2026-09-08.
- **Sparse tokenizer reuse.** SPLADE v3 is DistilBERT WordPiece; BGE-M3 is
  XLM-R/SentencePiece. Their token counts are not interchangeable, so the
  sparse chunker must never be used to bound dense input — that would be the
  same proxy failure in a new place.

## Recovery semantics

- The **23 vault reflections self-heal**: `_last_seen_hash` is set only after
  a successful create, so errors are uncached and reconcile retries on the
  next run. Expect a first post-restart pass of `140/140/0`.
- The **21 already-rejected episodic writes are NOT recoverable.** That path
  has no durable queue. This fix prevents future drops; it cannot
  reconstruct those.

## Status note

Frontmatter is `done` because this PR carries `Closes #734`, and the Vault
gate requires the post-merge state to be consistent at merge time (AGENTS
§Done). It is **not** a claim that review happened.

`reviewed:` stays **false**. Per
[[00-index/conventions]] that field means *"has Eric read and mentally
accepted this?"* — it is Eric's acceptance of the slice document, and no
reviewer's approval flips it. An earlier revision of this note wrongly
instructed the code reviewer to set it; corrected on Yua's must-fix.

Two separate gates, neither of which is `reviewed:`:

- **Code review** — Yua, independent, on PR #735.
- **Release gate** — Yua, separately owned: publish, digest pin, deploy.

## Work log

**2026-09-08 — `claude-code-opus5` (Aoi)**

Found by Shiori (reconcile error set + root cause in `tei.py:73` /
`chunked.py:120`). Verified independently, scoped, and planned by Aoi. Fix
approach (`truncate=true` rather than client-side BGE tokenization), live
proof, and release path by Yua. Green-lit by Eric.

An earlier Aoi proposal — lower `_DEFAULT_MAX_INPUT_CHARS_DENSE` to ~24,000
as a stopgap — was **withdrawn as unsound**: it was derived from an assumed
3.894 chars/token that came from an arithmetic error (a ratio computed from a
truncation that had not occurred). Yua's measured 2.10 chars/token showed
24,000 chars is still ~11,400 tokens. Recorded because the wrong number
nearly shipped.

Diff: `src/musubi/embedding/tei.py` +15/-1; `tests/test_embedding.py` +5
tests.

Evidence:
- `make check` green — 2691 passed, 195 skipped, 2 xfailed.
- Red proof: on 49e348c, `test_dense_embed_request_sets_truncate_true` failed
  `KeyError: 'truncate'`; the four controls passed.
- **Matched live canary**, identical 168,000-char token-dense input against
  the deployed BGE-M3 (`text-embeddings-inference:86-1.2.0`,
  `--model-id BAAI/bge-m3`), run inside `musubi-core-1`:
  - shipped client (in the running image) → `EmbeddingError: TEI returned
    413 ... Given: 10668`
  - patched client (this PR) → OK, 1 vector, dim 1024
  Non-persisting (`/embed` is stateless); probe files removed afterwards.
