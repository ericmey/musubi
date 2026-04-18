---
title: Research Questions
section: 00-index
type: index
status: complete
tags: [section/index, status/complete, type/index]
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# Research Questions

The consolidated pipeline of open research questions across the vault. Every item here blocks at least one spec from moving from `draft` or `research-needed` → `complete`. Once answered, promote to an ADR or fold into the relevant spec and cross out here.

Task emoji legend (parsed by the [Tasks plugin](https://publish.obsidian.md/tasks/)): `[R]` = Research, `[/]` = In progress, `[x]` = Resolved.

Browse live: [[_bases/research-stubs]].

## Live index of research-needed notes

```dataview
TABLE WITHOUT ID
  file.link AS "Note",
  section AS "Section",
  status AS "Status",
  updated AS "Updated"
FROM ""
WHERE status = "research-needed" AND !contains(file.folder, "_templates") AND !contains(file.folder, "_bases") AND !contains(file.folder, "_inbox")
SORT section ASC
```

## Open research tasks across all notes

```tasks
not done
status.name includes Research
short mode
group by folder
```

## By section

### 04 — Data model

- [R] Should `content` on episodic be compressed at rest (zstd)? Not worth it at current scale; revisit at 10M+ points. Source: [[04-data-model/episodic-memory]].
- [R] Store a raw transcript sample even when summarized? Current answer: no — use an artifact. Revisit after observing real retrieval failures. Source: [[04-data-model/episodic-memory]].
- [R] OCR for image-bearing PDFs — Tesseract or TrOCR on GPU? Post-v1. Source: [[04-data-model/source-artifact]].
- [R] Audio artifacts: do we ship a speech-to-text pipeline, or keep it adapter-owned? Source: [[04-data-model/source-artifact]].
- [R] Multi-part artifacts (PDF + spreadsheet) — introduce artifact collections, or keep `derived_from` links? Source: [[04-data-model/source-artifact]].
- [R] Should humans create `SynthesizedConcept` objects directly? Current answer: no. Lock decision into ADR. Source: [[04-data-model/synthesized-concept]].
- [R] Vault frontmatter identity-field behavior when a human edits `id` or `promoted-from`. Source: [[04-data-model/vault-schema]].

### 05 — Retrieval

- [R] Curate golden-set queries for each namespace + modality; aim for 100 per. Source: [[05-retrieval/evals]].
- [R] RAGAS integration — which metrics (precision, recall, faithfulness) graduate from "computed" to "gating"? Source: [[05-retrieval/evals]].
- [R] A/B test harness for shadow evals — standalone process or part of the Lifecycle Engine? Source: [[05-retrieval/evals]].
- [R] Clustering algorithm for concept synthesis — HDBSCAN vs agglomerative vs ad-hoc similarity threshold? Source: [[06-ingestion/concept-synthesis]].
- [R] Fact-extraction prompt template — Qwen2.5-7B vs a smaller model? Evaluate on a gold set. Source: [[06-ingestion/concept-synthesis]].

### 06 — Ingestion

- [R] Content-hash validation strategy on vault-sync: blake3 per chunk, or a whole-file digest? Source: [[06-ingestion/vault-sync]].

### 09 — Operations

- [R] GPU OOM recovery runbook — full procedure for CUDA OOM mid-request including evict + warmup sequence. Source: [[09-operations/runbooks]].
- [R] Qdrant corruption recovery — stepped procedure differentiating payload-only vs vector-index corruption. Source: [[09-operations/runbooks]].
- [R] Automated restore drill — what cadence; what succeeds/fails; where does it report? Source: [[09-operations/backup-restore]].
- [R] Scale-signal detection — which metric crosses what threshold to trigger a single-host → multi-host review? Source: [[09-operations/capacity]].

### 10 — Security

- [R] Prompt-injection detection patterns — regex, classifier, or both? Source: [[10-security/prompt-hygiene]].
- [R] Audit log ingestion pipeline — structured JSON to Loki, or straight to disk? Source: [[10-security/audit]].
- [R] SIEM integration (post-v1) — likely Wazuh or Elastic; defer until there's a second tenant. Source: [[10-security/audit]].

### 11 — Migration

- [R] Pydantic migration playbook — per-collection or big-bang? Source: [[11-migration/phase-1-schema]].

### 12 — Roadmap (v2/v3)

- [R] Multi-host conflict resolution strategy (both operators edit the same curated doc). Source: [[12-roadmap/phased-plan]].
- [R] Multi-host discovery (how does one presence find another Musubi). Source: [[12-roadmap/phased-plan]].
- [R] Multi-host trust model (prevent a malicious peer from reading unauthorized namespaces). Source: [[12-roadmap/phased-plan]].

## Resolution path

1. Pick a question. Convert it into a research note under `_inbox/research/<slug>.md` via the `research-question` template.
2. Spend ≤ 1 day on prior art + a small prototype.
3. Answer it. Fold the answer into the relevant spec and either:
   - Produce an ADR in [[13-decisions/index]], **or**
   - Drop the question here with `[x]` and link to the spec it closed.
4. Flip the source spec's `status` to `complete`.

## How this stays current

When a spec is flipped from `research-needed` or `draft` to `complete`, remove its questions here (or mark them `[x]`). The Base at [[_bases/research-stubs]] auto-updates from frontmatter.
