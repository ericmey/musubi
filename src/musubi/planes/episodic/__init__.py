"""Episodic plane — time-indexed recollection.

See [[04-data-model/episodic-memory]] for the spec. The plane is the single
write path into ``musubi_episodic`` — dedup-on-create, lifecycle
transitions, namespace isolation all live here.
"""

from musubi.planes.episodic.plane import EpisodicPlane

__all__ = ["EpisodicPlane"]
