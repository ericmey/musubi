"""Production-grade storage client factories.

Today: Qdrant. Future home for any backend wrappers that need to encode
configuration choices (cert trust, retry policy, instrumentation) in
one place instead of scattering them across call sites.
"""

from musubi.storage.qdrant_factory import build_qdrant_client

__all__ = ["build_qdrant_client"]
