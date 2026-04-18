---
title: "ADR Template: Scoring-Weight Change"
section: 13-decisions
tags: [adr, retrieval, scoring, section/decisions, status/complete, template, type/adr]
type: adr
status: complete
updated: 2026-04-17
up: "[[13-decisions/index]]"
reviewed: false
---
# ADR Template: Scoring-Weight Change

Use this template when changing any weight in [[05-retrieval/scoring-model]]. Copy into a new file `NNNN-weights-<short-reason>.md` and fill in.

Weight changes are tiny ADRs — a few sentences each — but we write one every time because the scoring formula is the user-visible "taste" of retrieval. Drift without a record is how nobody understands retrieval quality six months later.

## Template

```markdown
---
title: "ADR NNNN: Retrieval Weight Change — <reason>"
section: 13-decisions
tags: [adr, scoring, weights]
---

# ADR NNNN: Retrieval Weight Change — <reason>

**Status:** accepted
**Date:** YYYY-MM-DD
**Deciders:** Eric (+ anyone else)

## Context

What prompted the change? User complaint about stale results? Eval regression? New plane added?

## Decision

| Component | Old | New |
|---|---|---|
| α_semantic | 0.6 | 0.55 |
| β_recency | 0.15 | 0.20 |
| γ_importance | 0.15 | 0.15 |
| δ_provenance | 0.10 | 0.10 |

Effective date in code: commit `<sha>`.

## Evidence

Eval report link + summary:

- nDCG@10 on golden set: before X.XX → after X.XX.
- Notable query shifts: <bullet list of 3-5 queries whose top results changed materially>.

## Rollback

If regression observed within 7 days, revert to old weights via commit `<sha>`. No data migration required — weights are config.

## Links

- [[05-retrieval/scoring-model]]
- [[05-retrieval/evals]]
```

## Checklist before committing a weight change ADR

- [ ] Evals run on the canonical golden set; report attached.
- [ ] At least 3 hand-verified queries inspected for the new weights.
- [ ] Rollback path identified (usually the prior commit).
- [ ] Documented in `05-retrieval/scoring-model.md` current-weights table.
- [ ] Announced in `ops` presence so agents using blended retrieval know.

## Why a template, not a policy?

We don't want a committee to approve every weight tweak. The ADR itself is the approval — writing it down forces thought. Template keeps the friction low.

## Links

- [[05-retrieval/scoring-model]]
- [[05-retrieval/evals]]
- [[13-decisions/index]]
