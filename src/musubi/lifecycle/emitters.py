"""Adapter: lifecycle sweeps → `ThoughtsPlane`.

Background: the demotion / promotion / reflection sweeps depend on a
`ThoughtEmitter` Protocol with a small, channel-oriented surface:

    async def emit(self, channel: str, content: str, title: str | None = None) -> None

`ThoughtsPlane.send`, the production surface, takes a fully-shaped
`Thought` object. The two don't meet naturally — an adapter sits
between them so the sweeps can stay agnostic of ``Thought`` internals
(namespace shape, importance default, from/to presences) and
``ThoughtsPlane`` stays the single mutation path for the thoughts
collection.

Configuration:

- ``from_presence``: the adapter's own identity (e.g.
  ``"lifecycle-worker"``). Shows up as the author of every thought
  the sweeps emit.
- ``namespace``: the tenant-scoped bucket the thoughts land in.
  Defaults to ``<from_presence>/ops``.
- ``to_presence``: `"all"` by default (ops-channel broadcast). Can be
  overridden per-call if a sweep ever needs a targeted recipient.

The adapter does NOT retry. If `ThoughtsPlane.send` raises, the
caller decides — in practice the demotion sweep wraps each item in
its own try/except and logs failures rather than aborting the whole
batch.
"""

from __future__ import annotations

from musubi.planes.thoughts.plane import ThoughtsPlane
from musubi.types.thought import Thought


class ThoughtsPlaneEmitter:
    """Concrete `ThoughtEmitter` backed by :class:`ThoughtsPlane`.

    Example::

        emitter = ThoughtsPlaneEmitter(
            thoughts=thoughts_plane,
            from_presence="lifecycle-worker",
            namespace="ops/lifecycle/thoughts",
        )
        await emitter.emit("ops-alerts", "Concept X demoted")
    """

    def __init__(
        self,
        *,
        thoughts: ThoughtsPlane,
        from_presence: str,
        namespace: str | None = None,
        to_presence: str = "all",
    ) -> None:
        if not from_presence:
            raise ValueError("from_presence is required")
        self._thoughts = thoughts
        self._from = from_presence
        # Namespace format is strictly `<tenant>/<presence>/<plane>`
        # where plane ∈ {episodic, curated, concept, artifact, thought,
        # lifecycle} (enforced by MusubiObject). Default the
        # thoughts-plane namespace to `<from_presence>/ops/thought`.
        self._namespace = namespace or f"{from_presence}/ops/thought"
        self._to = to_presence

    async def emit(self, channel: str, content: str, title: str | None = None) -> None:
        """Send a thought to ``channel``.

        `title` is folded into the content when present — `Thought`
        doesn't have a title field (it's a message, not a memory
        row), and losing the distinction in the body keeps the
        wire-format honest.
        """
        body = f"[{title}] {content}" if title else content
        thought = Thought(
            namespace=self._namespace,
            content=body,
            from_presence=self._from,
            to_presence=self._to,
            channel=channel,
            importance=5,
        )
        await self._thoughts.send(thought)


__all__ = ["ThoughtsPlaneEmitter"]
