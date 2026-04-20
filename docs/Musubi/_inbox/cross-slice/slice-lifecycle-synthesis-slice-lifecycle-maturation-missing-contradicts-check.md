---
title: "Cross-slice: concept_maturation_sweep should respect contradicts list"
section: _inbox/cross-slice
type: cross-slice
status: resolved
tags: [section/inbox-cross-slice, type/cross-slice, status/resolved]
updated: 2026-04-19
---

## Resolution

Fixed by operator chore PR on 2026-04-19 — see v2 commit for `src/musubi/lifecycle/maturation.py` adding the contradicts-list check in `concept_maturation_sweep`. Test `test_synthesized_blocked_from_maturing_with_contradiction` in `tests/lifecycle/test_synthesis.py` un-skipped as part of the same fix.

Original ticket preserved below for audit.

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
