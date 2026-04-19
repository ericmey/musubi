"""Embedding clients and protocol.

This subpackage is the boundary between Musubi and the TEI / reranker GPU
services. Nothing in ``musubi.retrieve`` or ``musubi.planes`` talks to TEI
directly — they go through :class:`Embedder`. That lets tests swap in
:class:`FakeEmbedder` without the rest of the code noticing.

Public surface:

- :class:`Embedder` — runtime-checkable protocol with ``embed_dense``,
  ``embed_sparse``, ``rerank``.
- :class:`FakeEmbedder` — deterministic in-process double for unit tests.
- :class:`CachedEmbedder` — in-memory query embedding cache wrapper.
- :class:`TEIDenseClient`, :class:`TEISparseClient`, :class:`TEIRerankerClient`
  — thin httpx-backed TEI clients.
- :class:`EmbeddingError` — typed error raised by the TEI clients on
  5xx responses or network failures.

Specs realised:

- [[06-ingestion/embedding-strategy]] — named vectors, batching.
- [[08-deployment/gpu-inference-topology]] — TEI service layout.
"""

from musubi.embedding.base import Embedder, EmbeddingError
from musubi.embedding.cache import CachedEmbedder
from musubi.embedding.fake import FakeEmbedder
from musubi.embedding.tei import TEIDenseClient, TEIRerankerClient, TEISparseClient

__all__ = [
    "CachedEmbedder",
    "Embedder",
    "EmbeddingError",
    "FakeEmbedder",
    "TEIDenseClient",
    "TEIRerankerClient",
    "TEISparseClient",
]
