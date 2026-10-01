"""Compatibility imports for the extracted musubi-livekit package.

Install musubi-livekit to use this legacy import path. New code should import
from musubi_livekit directly.
"""

try:
    from musubi_livekit.adapter import LiveKitAdapter
except ModuleNotFoundError as exc:
    if exc.name == "musubi_livekit":
        raise ModuleNotFoundError("Install musubi-livekit to use musubi.adapters.livekit") from exc
    raise

__all__ = ["LiveKitAdapter"]
