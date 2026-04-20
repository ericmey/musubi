"""LiveKit voice adapter — Fast Talker + Slow Thinker dual-agent pattern.

Per [[07-interfaces/livekit-adapter]]. Embeds into a LiveKit Agents
worker as a Python package; not a standalone service. Wires LiveKit
session events (``on_user_turn_completed`` etc.) to Musubi
operations through the SDK's :class:`AsyncMusubiClient`-shaped
contract.

Adapters import this package as::

    from musubi.adapters.livekit import (
        LiveKitAdapter, LiveKitAdapterConfig,
        SlowThinker, FastTalker, ContextCache,
    )

Note: the spec at [[07-interfaces/livekit-adapter]] still refers to
this code as the sibling repo ``musubi-livekit-adapter``. ADR-0015 +
ADR-0016 moved it to ``src/musubi/adapters/livekit/`` inside the
monorepo; the spec is updated in-PR with a ``spec-update:`` trailer
when this slice lands.
"""

from musubi.adapters.livekit.adapter import LiveKitAdapter
from musubi.adapters.livekit.cache import ContextCache
from musubi.adapters.livekit.config import LiveKitAdapterConfig
from musubi.adapters.livekit.fast_talker import FastTalker
from musubi.adapters.livekit.heuristics import detect_interesting_fact
from musubi.adapters.livekit.redaction import redact_pii
from musubi.adapters.livekit.slow_thinker import SlowThinker

__all__ = [
    "ContextCache",
    "FastTalker",
    "LiveKitAdapter",
    "LiveKitAdapterConfig",
    "SlowThinker",
    "detect_interesting_fact",
    "redact_pii",
]
