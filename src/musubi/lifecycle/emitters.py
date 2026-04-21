"""Adapter: lifecycle sweeps → `ThoughtsPlane` + `VaultWriter`.

Background: the demotion / promotion / reflection sweeps depend on
small, Protocol-shaped surfaces for emitting thoughts and writing
files:

- demotion / promotion:
      async def emit(channel: str, content: str, title: str | None) -> None
- reflection:
      async def emit(*, namespace: str, channel: str, content: str, importance: int) -> None
- promotion: sync ``VaultWriter.write_curated``
- reflection: async ``VaultWriter.write_reflection``

`ThoughtsPlane.send`, the production surface, takes a fully-shaped
`Thought` object. The two don't meet naturally — adapters sit between
them so the sweeps can stay agnostic of ``Thought`` internals
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

import asyncio
from pathlib import Path

from musubi.planes.thoughts.plane import ThoughtsPlane
from musubi.types.thought import Thought
from musubi.vault.writelog import WriteLog


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


class ReflectionThoughtsEmitter:
    """Adapter for the reflection sweep's keyword-only `ThoughtEmitter`.

    The reflection Protocol passes namespace + importance explicitly
    per-call (the sweep is per-namespace), so we skip the
    `from_presence/ops/thought` defaulting that :class:`ThoughtsPlaneEmitter`
    does.
    """

    def __init__(
        self,
        *,
        thoughts: ThoughtsPlane,
        from_presence: str = "lifecycle-worker",
        to_presence: str = "all",
    ) -> None:
        if not from_presence:
            raise ValueError("from_presence is required")
        self._thoughts = thoughts
        self._from = from_presence
        self._to = to_presence

    async def emit(
        self,
        *,
        namespace: str,
        channel: str,
        content: str,
        importance: int,
    ) -> None:
        thought = Thought(
            namespace=namespace,
            content=content,
            from_presence=self._from,
            to_presence=self._to,
            channel=channel,
            importance=importance,
        )
        await self._thoughts.send(thought)


class ReflectionVaultWriter:
    """Adapter for the reflection sweep's async `write_reflection`.

    Wraps the sync :class:`musubi.vault.writer.VaultWriter` write path:
    concatenates frontmatter + body, writes the file, records into the
    shared :class:`WriteLog` so the vault watcher's echo-filter catches
    our own writes. ``asyncio.to_thread`` keeps the filesystem call off
    the event loop.
    """

    def __init__(
        self,
        *,
        vault_root: Path,
        write_log: WriteLog,
    ) -> None:
        self._vault_root = vault_root
        self._write_log = write_log

    async def write_reflection(self, *, path: str, frontmatter: str, body: str) -> None:
        def _sync_write() -> None:
            full = self._vault_root / path.lstrip("/")
            full.parent.mkdir(parents=True, exist_ok=True)
            content = frontmatter + body
            full.write_text(content, encoding="utf-8")
            # Echo-filter bookkeeping: the watcher sees this write but
            # consume_if_exists will drop it.
            import hashlib

            body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
            self._write_log.record_write(path, body_hash)

        await asyncio.to_thread(_sync_write)


__all__ = [
    "ReflectionThoughtsEmitter",
    "ReflectionVaultWriter",
    "ThoughtsPlaneEmitter",
]
