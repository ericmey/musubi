---
title: Reflection
section: 06-ingestion
tags: [digest, ingestion, reflection, section/ingestion, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[06-ingestion/index]]"
reviewed: false
implements: "tests/lifecycle/test_reflection.py"
---
# Reflection

A daily background pass that summarises the last day's memory and writes a digest to the vault. It borrows the "reflection" step from Stanford's Generative Agents ([https://arxiv.org/abs/2304.03442](https://arxiv.org/abs/2304.03442)), adapted to Musubi's planes and to a human reader.

Reflection (a summary for humans) is deliberately separate from synthesis (concepts for retrieval). They serve different readers at different tempos.

Code: `src/musubi/lifecycle/reflection.py`, `src/musubi/llm/reflection_client.py`.

## Purpose

- One place to see what was captured in the last 24 hours.
- Surface what was promoted, demoted or contradicted, and what is at risk of demotion.
- Produce a file a person can review, edit or archive.
- Produce a retrieval target: reflection files are indexed in the curated plane.

## Schedule

- **Daily at 06:00 UTC** (`reflection_digest` job), lock `<lock dir>/reflection.lock`.
- There is no CLI or API to re-run it on demand.

The window is the 24 hours ending at run time.

## Namespace

The worker runs reflection for **one namespace per deployment**, hard-coded in `src/musubi/lifecycle/runner.py` as `lifecycle-worker/ops/curated`. There is no per-tenant reflection yet. The data sections scroll Qdrant across all namespaces; they are not filtered to a tenant.

## Output

One markdown file per day at:

```
<VAULT_PATH>/vault/reflections/YYYY-MM/YYYY-MM-DD.md
```

The relative path from `vault_path_for()` is `vault/reflections/...`, written under the configured `VAULT_PATH`.

Frontmatter (from `render_frontmatter`):

```yaml
---
object_id: <ksuid>
namespace: lifecycle-worker/ops/curated
schema_version: 1
title: "Reflection — 2026-04-17"
topics:
  - reflection
tags: [reflection, daily]
importance: 6
state: matured
version: 1
musubi-managed: true
created: 2026-04-17T06:00:00+00:00
updated: 2026-04-17T06:00:00+00:00
---

# Reflection — 2026-04-17

## Capture summary
## Surfaced patterns
## Promotion candidates
### Promoted
### Skipped (gate passed, not promoted)
## Demotion candidates
### at-risk
## Contradictions
### New
### Resolved
## Worth revisiting
```

The same content is indexed as a `musubi_curated` row with `topics: [reflection]` and `tags: [reflection, daily]`. Reflections are **not** excluded from retrieval by default; there is no topic-exclusion filter on the retrieve API.

## Sections

### 1. Capture summary

Counts of rows created in the window, from Qdrant scrolls of the episodic, artifact and thought planes.

### 2. Surfaced patterns

The only LLM call. The sweep collects the window's episodic rows (id, content, importance, topics) and asks for 3–5 themes, one `##` section each, citing memory ids. Prompt: `src/musubi/llm/prompts/reflection/v1.txt`, temperature 0.2.

`validate_cited_ids` checks every cited id against the episodic ids actually in the window, and strips or annotates unknown ones, so a hallucinated id never reaches the file as if it were real.

**LLM endpoint:** `HttpxReflectionClient` posts to `{OLLAMA_URL}/api/chat` with `LLM_MODEL`. It does not use the `LIFECYCLE_LLM_*` settings (ADR 0043).

### 3. Promotion candidates

From the LifecycleEvent log in the window: concepts that were promoted, and concepts whose gate passed but which the promotion job did not promote.

### 4. Demotion candidates

Rows demoted in the window (from the event log, with reason), plus an **at-risk** list: matured episodic rows with importance ≤ 4 and no update for at least 30 days (`ReflectionConfig.at_risk_importance_max`, `at_risk_age_days_min`).

### 5. Contradictions

Contradiction state on concepts updated in the window, split into new links ("New") and resolved ones ("Resolved").

### 6. Worth revisiting

Curated rows with importance ≥ 8 not accessed for at least 30 days (`revisit_min_importance`, `revisit_min_age_days`), with days since last access. The aim is to prompt a person to refresh something important that has been forgotten.

## Flow

```
gather capture summary, promotions, demotions + at-risk, contradictions, revisit
collect window episodic ids -> LLM patterns -> validate cited ids
render markdown + frontmatter
VaultWriter: record write-log entry, write file
CuratedPlane.create(...)            (vault_path-keyed, so a re-run updates one row)
emit Thought on channel "scheduler": "Daily reflection ready: [[vault/reflections/...]]"
```

The file write goes through the vault write-log, so the vault watcher does not re-ingest it.

## Handling LLM outage

If the LLM call fails or returns nothing:

- The patterns section becomes `> LLM was unavailable at reflection time; patterns section skipped.`
- Every other section renders normally.
- The file is still written and the curated row is still indexed.

## Idempotency

Re-running for the same date overwrites the same file. The curated plane dedups by `vault_path`, so Qdrant keeps a single row.

## Test Contract

**Module under test:** `src/musubi/lifecycle/reflection.py`

Sections:

1. `test_capture_summary_counts_correct`
2. `test_patterns_section_parses_llm_output`
3. `test_patterns_section_validates_cited_ids`
4. `test_promotion_section_lists_both_promoted_and_skipped`
5. `test_demotion_section_includes_at_risk`
6. `test_contradiction_section_separates_new_and_resolved`
7. `test_revisit_section_filters_by_importance_and_age`

Output:

8. `test_file_written_at_expected_path`
9. `test_frontmatter_has_musubi_managed_true`
10. `test_file_indexed_in_musubi_curated`
11. `test_run_emits_thought_to_operator_channel`

Degradation:

12. `test_ollama_outage_skips_patterns_section_only`
13. `test_llm_exception_falls_back_to_skip_notice`

Idempotency:

14. `test_rerun_same_date_overwrites_same_file`

Integration (skipped):

15. `integration: seed 100 memories across 24h, run reflection, file exists, sections populated, point indexed`
16. `integration: LLM-outage scenario — file generated with patterns-skipped notice`
