---
title: Reflection
section: 06-ingestion
tags: [digest, ingestion, reflection, section/ingestion, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[06-ingestion/index]]"
reviewed: false
---
# Reflection

A daily background pass that surfaces patterns in the last day's memory and writes a digest to the vault. Inspired by the "reflection" step in Stanford's Generative Agents ([https://arxiv.org/abs/2304.03442](https://arxiv.org/abs/2304.03442)), adapted to our planes and to a human reader.

We deliberately separate reflection (summary-for-humans) from synthesis (concept generation for retrieval). Different audiences, different tempos.

## Purpose

- Give the human a single place to see what was captured in the last 24h.
- Surface things the system is about to demote, promote, or that now contradict.
- Produce a shareable artifact that the user can review, react to (via edits), or archive.
- Produce a new retrieval target — reflection files are indexed and can resurface in future queries.

## Schedule

- **Daily**, 06:00 local (`REFLECTION_SCHEDULE`).
- Can be re-run on-demand: `musubi-cli reflection run --date 2026-04-17`.

## Output

One markdown file per day at:

```
/srv/musubi/vault/reflections/2026-04/2026-04-17.md
```

With frontmatter:

```yaml
---
object_id: <ksuid>
namespace: eric/_shared/curated
schema_version: 1
title: "Reflection — 2026-04-17"
topics:
  - reflection
tags: [reflection, daily]
importance: 6
state: matured
version: 1
musubi-managed: true
created: 2026-04-17T06:00:00Z
updated: 2026-04-17T06:00:00Z
---

# Reflection — 2026-04-17

## Capture summary
...
## Surfaced patterns
...
## Promotion candidates
...
## Demotion candidates
...
## Contradictions
...
## Worth revisiting
...
```

Indexed in `musubi_curated` with `topics: [reflection]` — callers can opt in or out of including reflections in retrieval via `filters.topics_all=[reflection]` or `topics_any_exclude=[reflection]`.

## Sections

### 1. Capture summary

- **New memories captured** (by presence, by plane, by importance bucket).
- **New artifacts indexed** (count + total bytes).
- **New thoughts sent** (by channel).

Plain numbers + a few one-line exemplars ("Most important capture: 'CUDA 13 driver 575 installed' from claude-code-session").

### 2. Surfaced patterns

LLM-generated summary. Prompt:

```
Here are {N} matured memories captured in the last 24 hours, tagged by topic:
{list with title, topics, importance}

Identify 3-5 themes or patterns. For each:
- Theme (short title)
- 2-3 sentences on what's happening and why it's notable.
- Cite specific memory IDs.

Output structured markdown with one ## section per theme.
```

Small LLM pass (Qwen2.5-7B, temperature 0.4, ~3s). Validates each cited ID exists.

### 3. Promotion candidates

Concepts that passed the promotion gate in the last 24h (not just the ones the promotion job actually promoted — some may have been skipped due to conflicts):

```yaml
- concept: 2W1e...  title: "CUDA 13 installation pattern"
  reinforcement: 5
  importance: 7
  status: promoted → curated/eric/_shared/infrastructure/gpu/cuda-13-installation.md
```

With a linked view for each promoted concept:

```
[[curated/eric/_shared/infrastructure/gpu/cuda-13-installation]]
```

### 4. Demotion candidates

Objects demoted in the last 24h + ones teetering (will be demoted within 7 days if no change):

```yaml
- memory: 2W1eZ...  title: "Old GPU configuration"
  demoted: 2026-04-17 02:05 UTC
  reason: decay-rule:untouched-low-importance

- memory: 2W1eW...  (at-risk)
  will_demote: ~ 2026-04-24
  reason: 57 days untouched + importance=3
```

The at-risk list is a gentle nudge — if the human cares about one of these, they can reinforce it by opening it, reading, or promoting it manually.

### 5. Contradictions

Active contradictions surfaced or resolved since yesterday's reflection:

```yaml
new:
  - pair: (2W1eA..., 2W1eB...)
    about: "GPU memory — is it 8GB or 10GB?"
    suggested: "Likely 10GB per recent session notes"
resolved:
  - pair: (2W1eC..., 2W1eD...)
    resolved_by: <human>  keep=C  reason="D described old setup"
```

### 6. Worth revisiting

Objects that haven't been accessed in a long time but are high importance + high reinforcement:

```yaml
- curated: [[curated/eric/_shared/projects/ship-dates]]
  last_accessed: 45 days ago
  importance: 9
  reinforcement: 12
  note: "consistently high-value; might want a refresher"
```

Intended to prompt the human to refresh a forgotten-but-important memory.

## Implementation

```python
# musubi/lifecycle/reflection.py

async def run(client, tei, ollama, *, now):
    date = (now or time.time())
    window = (date - 86400, date)

    summary = await gather_capture_summary(client, window)
    patterns = await llm_patterns(ollama, summary)
    promotions = await gather_promotions(client, window)
    demotions = await gather_demotions(client, window, lookahead_days=7)
    contradictions = await gather_contradictions(client, window)
    revisit = await gather_worth_revisiting(client, now)

    body = render_markdown(summary, patterns, promotions, demotions, contradictions, revisit)
    path = vault_path(date)
    await vault.write(path, frontmatter(date), body, musubi_managed=True)
    await emit_thought(
        to_presence="all",
        channel="scheduler",
        content=f"Daily reflection ready: [[{path}]]",
        importance=5,
    )
```

LLM is called once (for patterns); all other sections are data queries. Keeps wall-clock time ~5s typical.

## Interaction with retrieval

Reflection files are indexed. A query "what was I working on last week?" would surface multiple `reflection` topic matches, each linking back to specific memories.

Reflections are excluded from retrieval by default for ambient queries (filters.topics_all_exclude=["reflection"]) because they duplicate content from source memories. The caller opts in explicitly ("search reflections").

## Handling LLM outage

If Ollama is unavailable:

- Capture summary: still generated (data, no LLM).
- Patterns section: replaced with `> LLM was unavailable at reflection time; patterns section skipped.`
- Other sections: unaffected.
- File still written, frontmatter still valid.

## Idempotency

Running reflection twice for the same date: second run overwrites the file (same path, same date-keyed frontmatter). No duplicate.

## Test contract

**Module under test:** `musubi/lifecycle/reflection.py`

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

Degradation:

11. `test_ollama_outage_skips_patterns_section_only`

Idempotency:

12. `test_rerun_same_date_overwrites_same_file`

Integration:

13. `integration: seed 100 memories across 24h, run reflection, file exists, sections populated, point indexed`
14. `integration: LLM-outage scenario — file generated with patterns-skipped notice`
