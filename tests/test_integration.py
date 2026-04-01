"""
Integration tests — end-to-end flows with mocks.

Verifies the full store->recall and send->check->read cycles work together.
"""

import uuid
from unittest.mock import MagicMock

import pytest

from musubi.memory import memory_store, memory_recall
from musubi.thoughts import thought_send, thought_check, thought_read
from tests.conftest import FakePoint, FakeQueryResult


class TestStoreRecallCycle:
    def test_store_then_recall(self, mock_qdrant, mock_embed):
        # Store a memory
        store_result = memory_store(
            mock_qdrant,
            content="Pony hair colors break photorealism",
            type="feedback",
            agent="aoi",
            tags=["rendering", "pony"],
        )
        assert store_result["status"] == "stored"
        stored_id = store_result["id"]

        # Now set up mock to return that memory on recall
        mock_qdrant.query_points.return_value = FakeQueryResult(
            points=[
                FakePoint(
                    id=stored_id,
                    payload={
                        "content": "Pony hair colors break photorealism",
                        "type": "feedback",
                        "agent": "aoi",
                        "tags": ["rendering", "pony"],
                        "context": "",
                        "created_at": "2026-04-01T10:00:00",
                        "access_count": 0,
                    },
                    score=0.92,
                )
            ]
        )

        recall_result = memory_recall(
            mock_qdrant, query="pony hair color issues"
        )
        assert len(recall_result["memories"]) == 1
        assert recall_result["memories"][0]["id"] == stored_id
        assert "pony" in recall_result["memories"][0]["tags"]


class TestThoughtSendCheckReadCycle:
    def test_send_check_read(self, mock_qdrant, mock_embed):
        # Send a thought
        send_result = thought_send(
            mock_qdrant,
            content="Check the LoRA training results when you wake up",
            from_presence="aoi-terminal",
            to_presence="aoi-house",
        )
        assert send_result["status"] == "sent"
        thought_id = send_result["id"]

        # Set up mock to return the thought on check
        mock_qdrant.scroll.return_value = (
            [
                FakePoint(
                    id=thought_id,
                    payload={
                        "content": "Check the LoRA training results when you wake up",
                        "from_presence": "aoi-terminal",
                        "to_presence": "aoi-house",
                        "created_at": "2026-04-01T10:00:00",
                    },
                )
            ],
            None,
        )

        check_result = thought_check(mock_qdrant, my_presence="aoi-house")
        assert check_result["unread_count"] == 1
        assert check_result["thoughts"][0]["id"] == thought_id

        # Mark as read
        read_result = thought_read(mock_qdrant, thought_ids=[thought_id])
        assert read_result["status"] == "read"
        assert read_result["marked"] == 1
