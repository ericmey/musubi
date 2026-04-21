"""Tests for :mod:`musubi.lifecycle.emitters`.

The :class:`ThoughtsPlaneEmitter` is a tiny adapter; the contract it
has to keep is:

- Builds a valid :class:`Thought` (namespace format, from/to
  presences, non-empty content) before calling
  :meth:`ThoughtsPlane.send`.
- Uses the operator-supplied namespace when given, otherwise falls
  back to ``<from_presence>/ops/thoughts``.
- Folds ``title`` into the content when provided — `Thought` has no
  title field.
- Empty ``from_presence`` raises rather than producing a broken row.
- Propagates errors from `ThoughtsPlane.send` (no swallow).
"""

from __future__ import annotations

from typing import Any

import pytest

from musubi.lifecycle.emitters import ThoughtsPlaneEmitter
from musubi.types.thought import Thought


class _StubThoughtsPlane:
    """Captures every `send` call for inspection."""

    def __init__(self) -> None:
        self.sent: list[Thought] = []
        self.fail_with: Exception | None = None

    async def send(self, thought: Thought, **_: Any) -> Thought:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(thought)
        return thought


async def test_emit_sends_a_valid_thought() -> None:
    plane = _StubThoughtsPlane()
    emitter = ThoughtsPlaneEmitter(
        thoughts=plane,  # type: ignore[arg-type]
        from_presence="lifecycle-worker",
        namespace="eric/ops/thought",
    )
    await emitter.emit("ops-alerts", "Concept X demoted")
    assert len(plane.sent) == 1
    thought = plane.sent[0]
    assert thought.namespace == "eric/ops/thought"
    assert thought.channel == "ops-alerts"
    assert thought.from_presence == "lifecycle-worker"
    assert thought.to_presence == "all"
    assert thought.content == "Concept X demoted"


async def test_emit_folds_title_into_content() -> None:
    plane = _StubThoughtsPlane()
    emitter = ThoughtsPlaneEmitter(
        thoughts=plane,  # type: ignore[arg-type]
        from_presence="lifecycle-worker",
        namespace="eric/ops/thought",
    )
    await emitter.emit("ops", "body text", title="Alert")
    assert plane.sent[0].content == "[Alert] body text"


async def test_emit_uses_default_namespace_when_not_supplied() -> None:
    plane = _StubThoughtsPlane()
    emitter = ThoughtsPlaneEmitter(
        thoughts=plane,  # type: ignore[arg-type]
        from_presence="aoi",
    )
    await emitter.emit("broadcast", "hello")
    assert plane.sent[0].namespace == "aoi/ops/thought"


async def test_emit_targeted_recipient() -> None:
    plane = _StubThoughtsPlane()
    emitter = ThoughtsPlaneEmitter(
        thoughts=plane,  # type: ignore[arg-type]
        from_presence="lifecycle-worker",
        namespace="eric/ops/thought",
        to_presence="aoi",
    )
    await emitter.emit("direct", "hey aoi")
    assert plane.sent[0].to_presence == "aoi"


def test_missing_from_presence_raises() -> None:
    with pytest.raises(ValueError, match="from_presence"):
        ThoughtsPlaneEmitter(
            thoughts=_StubThoughtsPlane(),  # type: ignore[arg-type]
            from_presence="",
        )


async def test_emit_does_not_swallow_plane_errors() -> None:
    plane = _StubThoughtsPlane()
    plane.fail_with = RuntimeError("qdrant down")
    emitter = ThoughtsPlaneEmitter(
        thoughts=plane,  # type: ignore[arg-type]
        from_presence="lifecycle-worker",
        namespace="eric/ops/thought",
    )
    with pytest.raises(RuntimeError, match="qdrant down"):
        await emitter.emit("ops", "boom")
