"""Fixtures for ``tests/store/`` — the slice-qdrant-layout test suite.

The ``qdrant`` fixture uses qdrant-client's in-memory (``:memory:``) mode —
a real implementation, not a mock. Collection bring-up and sparse/dense vector
config roundtrip faithfully; payload indexes are no-ops in local mode, so for
index-level assertions we use a ``MagicMock`` client and assert call shape
instead.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    """A fresh in-memory Qdrant client. One per test — no state leaks."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def mock_client() -> MagicMock:
    """A MagicMock stand-in for ``QdrantClient``.

    Used by tests that need to assert the *shape* of calls (e.g., "ensure_indexes
    called create_payload_index with the right field_schema") independent of
    whether the target backend actually indexes.

    Default behavior:

    - ``get_collections`` returns an object with an empty ``.collections`` list
      (so ``ensure_collections`` thinks it's a virgin node).
    - ``get_collection`` returns an object with ``payload_schema = {}``.
    """
    client = MagicMock()
    client.get_collections.return_value = MagicMock(collections=[])
    client.get_collection.return_value = MagicMock(payload_schema={})
    return client
