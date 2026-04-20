---
title: LiveKit Adapter
section: 07-interfaces
tags: [adapter, interfaces, livekit, section/interfaces, status/complete, type/spec, voice]
type: spec
status: complete
updated: 2026-04-19
up: "[[07-interfaces/index]]"
reviewed: false
implements: ["src/musubi/adapters/livekit/", "tests/adapters/test_livekit.py"]
---
# LiveKit Adapter

Integrates Musubi into LiveKit voice agent workers. Implements the Slow Thinker / Fast Talker dual-agent pattern so voice retrieval is both fast (for speech generation) and deep (for planning).

**Layout note (ADR-0015 / ADR-0016):** the adapter ships in-monorepo as
the sub-package `src/musubi/adapters/livekit/`, importable as
`musubi.adapters.livekit`. Embedded into the LiveKit agent worker as a
Python package; not a standalone service.

## The dual-agent pattern

Conversational AI with RAG has a latency dilemma:

- Deep retrieval (with reranker, cross-plane blended) takes ~2s. That's too slow for speech generation.
- Fast retrieval (hybrid-only) is ~150ms. Fast enough for in-speech, but might miss better context.

**Dual agent answers both**:

```
User speaks
  │
  ├─► Fast Talker (speech generation loop)
  │    - every 200ms: check Slow Thinker's cache
  │    - missing → fast-path retrieval (150ms)
  │    - generate + speak
  │
  └─► Slow Thinker (context pre-fetch loop)
       - accumulate transcript
       - on pause: deep-path retrieval (2s)
       - write results into cache
       - repeat
```

Between turns, the Slow Thinker pre-fetches rich context; the Fast Talker uses it on the next speech generation. If the cache misses, the Fast Talker falls back to fast-path retrieval — still acceptable under budget.

Pattern inspired by the "think-in-advance" strategies in conversational AI research (Claude's voice mode and others) as of April 2026.

## Components

### `SlowThinker`

```python
class SlowThinker:
    def __init__(self, client: AsyncMusubiClient, namespace: str, cache: ContextCache):
        self.client = client
        self.namespace = namespace
        self.cache = cache
        self._task: asyncio.Task | None = None

    async def on_user_utterance_segment(self, transcript_so_far: str):
        # Cancel prior pre-fetch; start a new one with the latest transcript
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._prefetch(transcript_so_far))

    async def _prefetch(self, transcript: str):
        q = RetrievalQuery(
            namespace=self.namespace,
            query_text=transcript,
            mode="deep",
            limit=15,
            include_lineage=True,
        )
        results = await self.client.retrieve(q)
        self.cache.put(transcript, results, ttl=120)
```

Pre-fetch is idempotent and cancelable. If the user keeps talking, we keep restarting with the newest transcript.

### `FastTalker`

```python
class FastTalker:
    def __init__(self, client: AsyncMusubiClient, namespace: str, cache: ContextCache):
        self.client = client
        self.namespace = namespace
        self.cache = cache

    async def get_context(self, query_text: str) -> list[RetrievalResult]:
        cached = self.cache.get_best_match(query_text, threshold=0.8)
        if cached:
            return cached
        q = RetrievalQuery(
            namespace=self.namespace,
            query_text=query_text,
            mode="fast",
            limit=5,
        )
        return (await self.client.retrieve(q)).ok
```

`get_best_match` does a cheap semantic match (query_text vs cached transcripts) — often a substring or re-phrasing of the pre-fetch query works.

### `ContextCache`

In-memory, per-session, short-lived:

```python
class ContextCache:
    def __init__(self, max_entries: int = 10):
        self._entries: list[CacheEntry] = []

    def put(self, key: str, results: list[RetrievalResult], ttl: float): ...
    def get_best_match(self, query: str, threshold: float) -> list[RetrievalResult] | None: ...
```

~10 entries is plenty for a single conversation session. Entries expire by TTL or age-out past 10.

## Event mapping

LiveKit agent events → Musubi operations:

| LiveKit event | Adapter action | Musubi call |
|---|---|---|
| `transcript_segment_received` | Slow Thinker starts/restarts pre-fetch | `retrieve(mode="deep")` |
| `turn_ended` | Slow Thinker does a final pre-fetch | `retrieve(mode="deep")` |
| `agent_speaks_context_needed` | Fast Talker checks cache / fast path | `retrieve(mode="fast")` |
| `session_ends` | Capture session transcript as artifact | `artifacts.upload()` |
| `interesting_fact_detected` (heuristic) | Capture episodic memory | `memories.capture()` |

`interesting_fact_detected` is optional — we can detect patterns ("remember…", "I always forget…") or let the agent explicitly mark "save this to memory".

## Session → artifact capture

At session end, the full session transcript is uploaded:

```python
async def on_session_end(session: LiveKitSession):
    vtt = session.to_vtt()   # WebVTT transcript
    artifact = await client.artifacts.upload(
        namespace=f"eric/_shared/artifact",
        title=f"Voice session {session.id}",
        content_type="text/vtt",
        source_system="livekit-session",
        source_ref=session.id,
        file=vtt.encode("utf-8"),
    )
    # Optionally: capture a summary thought
    await client.thoughts.send(
        from_presence="livekit-voice",
        to_presence="all",
        channel="scheduler",
        content=f"Session {session.id} captured as artifact {artifact.object_id}.",
    )
```

Transcripts are chunked server-side via `vtt-turns-v1`. See [[04-data-model/source-artifact]].

## Namespace conventions

Per voice session:

- `namespace: eric/livekit-voice/episodic` — new memories captured during the session.
- `namespace: eric/livekit-voice/blended` — retrieval scope (expands to tenant-wide per [[05-retrieval/blended#blended-scope-for-the-voice-agent]]).

## Latency budget

| Operation | p50 | p95 |
|---|---|---|
| Fast Talker cache hit | 0ms | 0ms |
| Fast Talker fast-path miss | 150ms | 400ms |
| Slow Thinker deep-path | 2s | 5s |
| Session-end artifact upload | 100ms | 300ms |

Fast Talker's "cache hit" rate target: ≥ 60% on in-session queries. Measured; affects overall perceived latency.

## Error handling

- Fast Talker cache miss → fast path → Musubi 503 → the agent continues without context (logs a warning, speaks generically).
- Slow Thinker fails → cache stays empty → Fast Talker falls through → same as above.
- Artifact upload fails → retry 3x with backoff → if still failing, log + defer to a local queue, retry next session.

We never block speech on Musubi. Every retrieval call is wrapped in `asyncio.wait_for` with the fast-path budget.

## Privacy

Voice transcripts can be sensitive. Configurable per-adapter:

- `MUSUBI_LIVEKIT_CAPTURE_TRANSCRIPTS=false` disables artifact capture entirely.
- `MUSUBI_LIVEKIT_CAPTURE_FACTS=true` enables heuristic memory capture but skips transcripts.
- Redaction pass (PII) before upload: optional, off by default. See [[10-security/redaction]].

## Observability

- `livekit.retrieval.cache_hit` counter (layer: slow_thinker_cache | fast_talker_fallback).
- `livekit.slow_thinker.prefetch_cancelled` counter.
- `livekit.session.captured_bytes` histogram.
- `livekit.fact.captured` counter.

## Test Contract

**Module under test:** `src/musubi/adapters/livekit/*.py`

Pattern:

1. `test_slow_thinker_restarts_on_new_transcript_segment`
2. `test_slow_thinker_writes_cache_on_completion`
3. `test_slow_thinker_cancelled_during_user_interrupt`
4. `test_fast_talker_prefers_cache_over_fallback`
5. `test_fast_talker_fallback_on_cache_miss`
6. `test_cache_ttl_respected`
7. `test_cache_age_out_at_max_entries`

Events:

8. `test_transcript_segment_triggers_prefetch`
9. `test_turn_end_triggers_final_prefetch`
10. `test_session_end_uploads_artifact`
11. `test_heuristic_detects_interesting_fact`

Artifact capture:

12. `test_session_transcript_uploaded_as_vtt`
13. `test_upload_retries_on_transient_failure`
14. `test_upload_queue_persists_on_hard_failure`

Privacy:

15. `test_capture_disabled_env_flag_skips_all_writes`
16. `test_redaction_pass_removes_pii_if_enabled`

Integration:

17. `integration: mock LiveKit session → Musubi → results returned inside budget`
18. `integration: canonical contract suite passes via adapter`
19. `integration: artifact storage of 10-minute session completes < 500ms end-to-end`
