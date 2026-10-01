---
title: Context Pack
section: 05-retrieval
tags: [retrieval, context-pack, essence, section/retrieval, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[05-retrieval/index]]"
reviewed: false
implements: ["src/musubi/retrieve/context_pack.py", "src/musubi/api/routers/context.py", "src/musubi/cli/context.py", "tests/retrieve/test_context_pack.py", "tests/api/test_context.py", "tests/cli/test_cli_context.py"]
---
# Context Pack

`POST /v1/context` is Musubi's startup/readiness context surface. It is not a
generic search endpoint. It turns a task or moment into a small, grouped
context pack that an agent can inject before acting.

Musubi serves **essence alignment** rather than stale context dumps.

## Contract

Input:

```json
{
  "namespace": "alex/assistant",
  "query_text": "billing service release checklist",
  "mode": "startup",
  "planes": ["episodic", "curated", "concept"],
  "candidate_limit": 30,
  "max_items": 8,
  "max_chars": 1200,
  "include_history": false
}
```

Output is grouped:

- `Must-Obey`
- `Current-Project`
- `Relationship-Voice`
- `Open-Loops`
- `Tool-Runtime`
- `Recent-Corrections`
- `Context`

Each item includes:

- `kind` — one of the closed whitelist below.
- `staleness` — `durable`, `current`, `episodic`, or `superseded`.
- `content` — char-capped, prompt-ready text.
- `evidence_handle` — `<namespace>/<object_id>`.
- `why_surfaced` — short ranking reason.

## Closed Kind Whitelist

Only these typed kinds are accepted:

- `boundary`
- `operating-rule`
- `identity-principle`
- `relationship/care-cue`
- `project-stance`
- `open-loop`
- `tool/runtime-fact`
- `correction/suppression`
- `episode`

Legacy rows without a typed `kind:` tag adapt as `episode`.

New episodic captures default missing typed tags at the API boundary:

- missing `kind:*` -> `kind:episode`
- missing `staleness:*` -> `staleness:episodic`
- caller-supplied typed tags are preserved exactly

`kind:episode` is therefore the default classification for rows written through
`POST /v1/episodic`, not proof that the caller explicitly marked the row as an
episode. Future read filters should treat it as a broad episodic-plane default;
if a caller-intent distinction becomes necessary, add a separate provenance tag
or metadata field instead of overloading `kind:episode`.

Typed writes use tags:

```text
kind:project-stance
staleness:durable
```

Unknown `kind:` or `staleness:` tags are rejected at capture/patch time with a
422 request-validation error. Plain legacy tags remain legal.

## Ranking

The v1 ranker is BM25 lexical with token-overlap fallback/debug fields and a
future-compatible interface for semantic expansion later. Ranking happens
inside Musubi, not inside each client.

Priority order:

1. Kind/staleness policy: durable boundaries and operating rules beat shallow
   lexical overlap.
2. BM25 lexical score against the current task/moment.
3. Existing retrieval score.
4. Importance.
5. Recency as a final tiebreak.

RET-013 adds a bounded recent lane alongside the ranked lane so a newly written
provisional memory can carry context across modalities before maturation. The
recent lane is capped, deduplicated against ranked results by
`(namespace, plane, object_id)`, and cannot consume the entire item or character
budget when both lanes have candidates. Ranked metadata wins for duplicates.
Caller-provided state filters apply to both lanes; otherwise recent includes
`provisional`, `matured`, and `promoted`, while ranked includes `matured` and
`promoted`.

`superseded` and `correction/suppression` records are suppressed by default.
They are retrievable when the caller sets `include_history=true`.

Startup packs also avoid unrelated episodic filler: if a low-priority episode
has zero query overlap, it is not inserted just because the pack has room.

## Acceptance Scenarios

The tests encode these acceptance criteria:

- **Project task.** A startup pack for a project surfaces the project's current
  durable lessons and runtime facts; it does not surface superseded drift notes
  unless history is explicitly requested.
- **Conventions change.** After a team changes its communication tooling, the
  pack surfaces the canonical tools and the operating rule about them, and
  suppresses the retired practice by default.
- **Relationship moment.** When the moment calls for presence rather than a
  task, the pack surfaces the relevant relationship/care cue without filling
  the pack with project-management habits.

## Test Contract

1. `test_startup_pack_surfaces_current_lessons_not_superseded_drift`
  proves the v1 startup pack surfaces a project's current durable lessons and
  runtime facts while suppressing superseded drift.
2. `test_startup_pack_surfaces_canonical_comms_and_hides_retired_practice`
  proves canonical communication tools and the operating rule about them surface
  while the retired practice stays hidden.
3. `test_presence_moment_prefers_care_cues_over_pm_filler` proves
  relationship/care-cue memories outrank project-management filler.
4. `test_legacy_rows_default_to_episode_and_history_can_retrieve_superseded`
  proves legacy untyped rows remain readable and superseded rows require
  explicit history mode.
5. `test_durable_rule_beats_shallow_overlap_episode` proves durable boundaries
  beat shallow lexical overlap.
6. `test_context_endpoint_returns_grouped_server_ranked_pack` proves the API
  returns the server-ranked grouped pack through `/v1/context`.
7. `test_context_endpoint_can_include_history_when_explicitly_requested` proves
  the API history mode includes superseded rows.
8. `test_context_endpoint_blends_recent_provisional_with_established_ranked`
  proves the final pack reserves bounded room for newly written provisional
  memories without displacing established ranked context.
9. `test_context_endpoint_max_chars_mix_quota` proves the recent lane cannot
  consume the full character budget when both lanes have candidates.
10. `test_context_endpoint_single_lane_empty_cases` proves either lane can be
  empty without suppressing valid results from the other lane.
11. `test_context_endpoint_custom_state_filter_applies_to_both_lanes` proves an
  explicit caller state filter governs both orchestration requests.
12. `test_capture_rejects_unknown_typed_kind_tag` proves typed-write minimum
  rejects unknown `kind:` tags.
13. `test_capture_allows_legacy_untyped_tags` proves legacy tags are still
  accepted.
14. `test_context_posts_to_api_and_renders_grouped_output` proves the CLI calls
  the deployed API and renders grouped output.
15. `test_context_json_flag_emits_raw_response` proves the CLI can emit the raw JSON
  contract.

## CLI

Operators can call the deployed service with:

```bash
musubi context \
  --namespace alex/assistant \
  --query "billing service release checklist" \
  --planes episodic,curated,concept
```

There is also a direct entry point:

```bash
musubi-context --namespace alex/assistant --query "startup"
```

Both call `/v1/context`; neither reads local Qdrant.

## Deployment

Context packs are part of Musubi Core and ship with the core image; there is no
separate deployment step. Local green tests do not prove a deployment works for
its consumers: after upgrading, an operator should run their own consumer
checks — commands that exercise each real consumer of `/v1/context`, not just
Core health — and roll back the image if any fail.
