"""Tests for ``LifecycleEvent`` + the allowed-transition table."""

from __future__ import annotations

import pytest

from musubi.types import (
    LifecycleEvent,
    allowed_states,
    generate_ksuid,
    is_legal_transition,
    legal_next_states,
)


class TestTransitionTable:
    @pytest.mark.parametrize(
        ("object_type", "frm", "to", "ok"),
        [
            ("episodic", "provisional", "matured", True),
            ("episodic", "provisional", "archived", True),
            ("episodic", "matured", "demoted", True),
            ("episodic", "matured", "superseded", True),
            ("episodic", "demoted", "matured", True),
            ("episodic", "archived", "matured", True),
            ("episodic", "matured", "synthesized", False),
            ("episodic", "superseded", "matured", False),
            ("curated", "matured", "archived", True),
            ("curated", "matured", "superseded", True),
            ("curated", "matured", "demoted", False),
            ("concept", "synthesized", "matured", True),
            ("concept", "matured", "promoted", True),
            ("concept", "matured", "demoted", True),
            ("concept", "promoted", "matured", False),
            ("artifact", "matured", "archived", True),
            ("artifact", "matured", "demoted", False),
            ("thought", "provisional", "matured", True),
            ("thought", "matured", "archived", True),
            ("thought", "archived", "matured", False),
        ],
    )
    def test_table_entries(self, object_type: str, frm: str, to: str, ok: bool) -> None:
        assert is_legal_transition(object_type, frm, to) is ok  # type: ignore[arg-type]

    def test_unknown_object_type_returns_false(self) -> None:
        assert is_legal_transition("whatever", "matured", "archived") is False

    def test_legal_next_states_terminal_returns_empty(self) -> None:
        assert legal_next_states("episodic", "superseded") == frozenset()

    def test_allowed_states_for_each_type(self) -> None:
        assert "provisional" in allowed_states("episodic")
        assert "synthesized" in allowed_states("concept")
        assert "provisional" not in allowed_states("curated")
        assert "indexing" not in allowed_states("artifact")  # indexing is on the other axis
        with pytest.raises(ValueError, match="unknown object_type"):
            allowed_states("bogus")


class TestLifecycleEvent:
    def test_legal_transition_accepts(self) -> None:
        ev = LifecycleEvent(
            object_id=generate_ksuid(),
            object_type="episodic",
            namespace="eric/claude-code/episodic",
            from_state="provisional",
            to_state="matured",
            actor="maturation-worker",
            reason="hourly sweep",
        )
        assert ev.from_state == "provisional"
        assert ev.occurred_epoch is not None
        assert ev.occurred_epoch == ev.occurred_at.timestamp()

    def test_illegal_transition_rejected(self) -> None:
        with pytest.raises(ValueError, match="illegal transition"):
            LifecycleEvent(
                object_id=generate_ksuid(),
                object_type="episodic",
                namespace="eric/claude-code/episodic",
                from_state="matured",
                to_state="synthesized",
                actor="x",
                reason="y",
            )

    def test_unknown_object_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="illegal transition"):
            LifecycleEvent(
                object_id=generate_ksuid(),
                object_type="unknown-type",
                namespace="eric/claude-code/episodic",
                from_state="matured",
                to_state="demoted",
                actor="x",
                reason="y",
            )

    def test_empty_reason_rejected(self) -> None:
        with pytest.raises(ValueError):
            LifecycleEvent(
                object_id=generate_ksuid(),
                object_type="episodic",
                namespace="eric/claude-code/episodic",
                from_state="provisional",
                to_state="matured",
                actor="x",
                reason="",
            )

    def test_namespace_validated(self) -> None:
        with pytest.raises(ValueError, match="tenant/presence/plane"):
            LifecycleEvent(
                object_id=generate_ksuid(),
                object_type="episodic",
                namespace="bad",
                from_state="provisional",
                to_state="matured",
                actor="x",
                reason="y",
            )

    def test_roundtrip_json(self) -> None:
        ev = LifecycleEvent(
            object_id=generate_ksuid(),
            object_type="concept",
            namespace="eric/synth/concept",
            from_state="synthesized",
            to_state="matured",
            actor="concept-maturation",
            reason="24h without contradiction",
            lineage_changes={"merged_from_added": [generate_ksuid()]},
            correlation_id="req-abc",
        )
        restored = LifecycleEvent.model_validate_json(ev.model_dump_json())
        assert restored == ev


class TestStateMachineReachability:
    """Every declared transition is reachable from its declared source state."""

    def test_no_orphan_states_in_allowed_table(self) -> None:
        for obj_type in ("episodic", "curated", "concept", "artifact", "thought"):
            states = allowed_states(obj_type)
            for s in states:
                # legal_next_states returns a frozenset; no KeyError here
                legal_next_states(obj_type, s)


def test_capture_event_validates(sample_namespace: str) -> None:
    from musubi.types.common import generate_ksuid
    from musubi.types.lifecycle_event import CaptureEvent

    event = CaptureEvent(
        object_id=generate_ksuid(),
        object_type="episodic",
        namespace=sample_namespace,
        state="provisional",
        actor="user",
        reason="test capture",
    )
    assert event.state == "provisional"
    assert event.occurred_at is not None
    assert event.occurred_epoch is not None
