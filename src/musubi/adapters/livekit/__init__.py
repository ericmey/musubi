"""Compatibility imports for the extracted musubi-livekit package.

Install musubi-livekit to use this legacy import path. New code should import
from musubi_livekit directly.
"""

try:
    from musubi_livekit import (
        ContextCache,
        FastTalker,
        LiveKitAdapter,
        LiveKitAdapterConfig,
        SlowThinker,
        detect_interesting_fact,
        redact_pii,
    )
except ModuleNotFoundError as exc:
    if exc.name == "musubi_livekit":
        raise ModuleNotFoundError("Install musubi-livekit to use musubi.adapters.livekit") from exc
    raise

__all__ = [
    "ContextCache",
    "FastTalker",
    "LiveKitAdapter",
    "LiveKitAdapterConfig",
    "SlowThinker",
    "detect_interesting_fact",
    "redact_pii",
]
