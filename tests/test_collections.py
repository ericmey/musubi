"""Tests for collection setup."""

from unittest.mock import MagicMock, patch

import pytest

from musubi.collections import ensure_collections
from tests.conftest import FakeCollectionList


class TestEnsureCollections:
    def test_creates_when_missing(self):
        client = MagicMock()
        client.get_collections.return_value = FakeCollectionList(names=[])

        result = ensure_collections(client)
        assert result is True
        assert client.create_collection.call_count == 2

    def test_skips_when_exists(self):
        client = MagicMock()
        client.get_collections.return_value = FakeCollectionList(
            names=["musubi_memories", "musubi_thoughts"]
        )

        result = ensure_collections(client)
        assert result is True
        client.create_collection.assert_not_called()

    def test_creates_only_missing(self):
        client = MagicMock()
        client.get_collections.return_value = FakeCollectionList(
            names=["musubi_memories"]
        )

        result = ensure_collections(client)
        assert result is True
        assert client.create_collection.call_count == 1

    @patch("musubi.collections.time.sleep")
    def test_retries_on_connection_failure(self, mock_sleep):
        client = MagicMock()
        client.get_collections.side_effect = [
            Exception("connection refused"),
            Exception("connection refused"),
            FakeCollectionList(names=["musubi_memories", "musubi_thoughts"]),
        ]

        result = ensure_collections(client)
        assert result is True
        assert mock_sleep.call_count == 2

    @patch("musubi.collections.time.sleep")
    def test_returns_false_after_all_retries(self, mock_sleep):
        client = MagicMock()
        client.get_collections.side_effect = Exception("connection refused")

        result = ensure_collections(client)
        assert result is False
        assert mock_sleep.call_count == 4  # 5 attempts, 4 sleeps
