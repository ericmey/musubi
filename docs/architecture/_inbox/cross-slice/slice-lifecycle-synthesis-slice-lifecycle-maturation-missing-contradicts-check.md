---
title: "Cross-slice: concept_maturation_sweep should respect contradicts list"
section: _inbox/cross-slice
type: cross-slice
status: open
tags: [section/inbox-cross-slice, type/cross-slice, status/open]
updated: 2026-04-19
---

## Bug

`concept_maturation_sweep` in `src/musubi/lifecycle/maturation.py` does not skip concepts that have a non-empty `contradicts` list, violating the lifecycle spec.

## Fix

This fix was identified during the implementation of `slice-lifecycle-synthesis`. Owner: `slice-lifecycle-maturation`.

Apply the following 7-line fix to `src/musubi/lifecycle/maturation.py` inside `concept_maturation_sweep`:

```python
        if int(row.get("reinforcement_count", 0)) < cfg.concept_reinforcement_threshold:
            continue
        # Check for active contradictions
        contradicts = row.get("contradicts", [])
        if isinstance(contradicts, list) and len(contradicts) > 0:
            log.info("Skipping maturation for concept %s: has active contradictions", row["object_id"])
            continue
```
