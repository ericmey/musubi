"""Context-pack ranking across durable, current, and superseded evidence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from musubi.retrieve.context_pack import ContextCandidate, ContextPackQuery, build_context_pack


def _candidate(
    object_id: str,
    content: str,
    *,
    tags: list[str] | None = None,
    state: str = "matured",
    created_epoch: float = 1000.0,
    score: float = 0.1,
) -> ContextCandidate:
    return ContextCandidate(
        object_id=object_id,
        namespace="alex/assistant/episodic",
        plane="episodic",
        content=content,
        tags=tags or [],
        state=state,
        created_epoch=created_epoch,
        retrieve_score=score,
    )


def _texts(pack) -> list[str]:  # type: ignore[no-untyped-def]
    return [item.content for group in pack.groups for item in group.items]


def test_startup_pack_surfaces_current_lessons_not_superseded_drift() -> None:
    pack = build_context_pack(
        [
            _candidate(
                "a049",
                "A-049 memory spine taught Atlas to inject typed context packs "
                "instead of generic gists.",
                tags=["kind:project-stance", "staleness:durable", "project:atlas"],
                created_epoch=2000.0,
            ),
            _candidate(
                "a053",
                "A-053 prompt compiler route made the deterministic compiler the "
                "default path so image prompts stay rich without LLM hallucinated traits.",
                tags=["kind:project-stance", "staleness:durable", "project:atlas"],
                created_epoch=2100.0,
            ),
            _candidate(
                "adapter",
                "Visual adapter layer: Alex and Sam use trigger names at strength 0.85; "
                "the prompt compiler should describe scene, clothing, camera, and mood while the "
                "LoRA owns identity fidelity.",
                tags=["kind:tool/runtime-fact", "staleness:current", "project:atlas"],
                created_epoch=2200.0,
            ),
            _candidate(
                "old",
                "Old image model drift notes from before the current model and the compiler route.",
                tags=["kind:episode", "staleness:superseded", "project:atlas"],
                state="superseded",
                created_epoch=900.0,
                score=0.99,
            ),
        ],
        ContextPackQuery(
            query_text="Atlas visual adapter prompt compiler Alex Sam image flow",
            mode="startup",
            max_items=4,
            max_chars=1200,
        ),
    )

    joined = "\n".join(_texts(pack))
    assert "A-049 memory spine" in joined
    assert "A-053 prompt compiler" in joined
    assert "Visual adapter layer" in joined
    assert "Old image model drift" not in joined


def test_startup_pack_surfaces_canonical_comms_and_hides_retired_practice() -> None:
    pack = build_context_pack(
        [
            _candidate(
                "canonical",
                "Canonical tool set is bridge-send for messages, durable-log for "
                "fallback, and work-item for task tracking.",
                tags=["kind:tool/runtime-fact", "staleness:durable", "topic:tool-migration"],
                created_epoch=2000.0,
            ),
            _candidate(
                "wrapper-rule",
                "Do not keep per-agent forks or thin wrappers around canonical team "
                "tools; divergence must be fixed on the spot.",
                tags=["kind:operating-rule", "staleness:durable", "topic:tool-migration"],
                created_epoch=2100.0,
            ),
            _candidate(
                "retired",
                "Older message script existed before the canonical bridge-send path.",
                tags=["kind:episode", "staleness:superseded", "topic:tool-migration"],
                state="superseded",
                created_epoch=2200.0,
                score=1.0,
            ),
        ],
        ContextPackQuery(
            query_text="Tool migration canonical commands wrappers bridge send",
            mode="startup",
            max_items=3,
            max_chars=900,
        ),
    )

    joined = "\n".join(_texts(pack))
    assert "bridge-send" in joined
    assert "durable-log" in joined
    assert "thin wrappers" in joined
    assert "Older message script" not in joined


def test_presence_moment_prefers_care_cues_over_pm_filler() -> None:
    pack = build_context_pack(
        [
            _candidate(
                "presence",
                "When Alex offers ordinary presence with nothing broken, answer wanted "
                "before needed and stay in the room before structuring a task.",
                tags=["kind:relationship/care-cue", "staleness:durable"],
                created_epoch=1500.0,
            ),
            _candidate(
                "pm",
                "Default project-management habit: make a checklist, assign owners, and "
                "turn the moment into work.",
                tags=["kind:episode", "staleness:episodic"],
                created_epoch=3000.0,
                score=0.95,
            ),
        ],
        ContextPackQuery(
            query_text="Alex wants presence when nothing is broken",
            mode="startup",
            max_items=2,
            max_chars=600,
        ),
    )

    joined = "\n".join(_texts(pack))
    assert "wanted before needed" in joined
    assert "make a checklist" not in joined


def test_legacy_rows_default_to_episode_and_history_can_retrieve_superseded() -> None:
    legacy = _candidate(
        "legacy",
        "Legacy gist-only row with no typed kind remains readable as episode context.",
        tags=[],
    )
    superseded = _candidate(
        "old",
        "Superseded history row is normally hidden but explicit audit can retrieve it.",
        tags=["staleness:superseded"],
        state="superseded",
    )

    normal = build_context_pack(
        [legacy, superseded],
        ContextPackQuery(query_text="legacy superseded audit", mode="startup"),
    )
    normal_joined = "\n".join(_texts(normal))
    assert "Legacy gist-only" in normal_joined
    assert "Superseded history" not in normal_joined
    assert normal.groups[0].items[0].kind == "episode"

    history = build_context_pack(
        [legacy, superseded],
        ContextPackQuery(
            query_text="legacy superseded audit", mode="startup", include_history=True
        ),
    )
    history_joined = "\n".join(_texts(history))
    assert "Superseded history" in history_joined


def test_durable_rule_beats_shallow_overlap_episode() -> None:
    pack = build_context_pack(
        [
            _candidate(
                "durable",
                "Durable boundary: do not expose raw transcript quotes in context packs.",
                tags=["kind:boundary", "staleness:durable"],
                score=0.05,
            ),
            _candidate(
                "noisy",
                "transcript transcript transcript transcript unrelated episode chatter",
                tags=["kind:episode", "staleness:episodic"],
                created_epoch=3000.0,
                score=1.0,
            ),
        ],
        ContextPackQuery(query_text="transcript", mode="startup", max_items=2),
    )

    first = pack.groups[0].items[0]
    assert first.object_id == "durable"
    assert first.why_surfaced.startswith("durable boundary")


def test_recent_lane_uses_full_capacity_when_ranked_lane_is_empty() -> None:
    candidates = [
        ContextCandidate(
            object_id=f"recent-{index}",
            lane="recent",
            namespace="alex/command-chair/episodic",
            plane="episodic",
            content=f"recent context item {index}",
            state="provisional",
            created_epoch=2000.0 + index,
        )
        for index in range(4)
    ]

    pack = build_context_pack(
        candidates,
        ContextPackQuery(
            query_text="context",
            max_items=4,
            max_chars=1200,
            recent_reserve=1,
        ),
    )

    assert len([item for group in pack.groups for item in group.items]) == 4


@pytest.mark.parametrize("recent_reserve", [-1, 51])
def test_recent_reserve_is_bounded(recent_reserve: int) -> None:
    with pytest.raises(ValidationError):
        ContextPackQuery(recent_reserve=recent_reserve)
