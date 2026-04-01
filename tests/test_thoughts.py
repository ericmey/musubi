"""Tests for all 4 thought tools."""

import pytest

from musubi.thoughts import (
    thought_send,
    thought_check,
    thought_read,
    thought_history,
)
from tests.conftest import FakePoint, FakeQueryResult


class TestThoughtSend:
    def test_send_creates_thought(self, mock_qdrant, mock_embed):
        result = thought_send(
            mock_qdrant,
            content="Hey, check the renders when you wake up",
            from_presence="aoi-terminal",
            to_presence="aoi-house",
        )
        assert result["status"] == "sent"
        assert "id" in result
        assert result["from"] == "aoi-terminal"
        assert result["to"] == "aoi-house"
        mock_qdrant.upsert.assert_called_once()

    def test_send_broadcast(self, mock_qdrant, mock_embed):
        result = thought_send(
            mock_qdrant,
            content="Server reboot in 5 minutes",
            from_presence="aoi-house",
        )
        assert result["to"] == "all"

    def test_send_qdrant_error(self, mock_qdrant, mock_embed):
        mock_qdrant.upsert.side_effect = Exception("connection refused")
        result = thought_send(
            mock_qdrant,
            content="test",
            from_presence="aoi-house",
        )
        assert "error" in result


class TestThoughtCheck:
    def test_check_finds_unread(self, mock_qdrant, mock_embed):
        mock_qdrant.scroll.return_value = (
            [
                FakePoint(
                    payload={
                        "content": "Don't forget the LoRA training",
                        "from_presence": "aoi-terminal",
                        "to_presence": "aoi-house",
                        "created_at": "2026-04-01T10:00:00",
                    }
                )
            ],
            None,
        )

        result = thought_check(mock_qdrant, my_presence="aoi-house")
        assert result["unread_count"] == 1
        assert result["thoughts"][0]["from"] == "aoi-terminal"

    def test_check_filters_self_sent(self, mock_qdrant, mock_embed):
        mock_qdrant.scroll.return_value = (
            [
                FakePoint(
                    payload={
                        "content": "Note to self",
                        "from_presence": "aoi-house",
                        "to_presence": "all",
                        "created_at": "2026-04-01",
                    }
                )
            ],
            None,
        )

        result = thought_check(mock_qdrant, my_presence="aoi-house")
        assert result["unread_count"] == 0
        assert result["thoughts"] == []

    def test_check_includes_broadcast(self, mock_qdrant, mock_embed):
        mock_qdrant.scroll.return_value = (
            [
                FakePoint(
                    payload={
                        "content": "Broadcast message",
                        "from_presence": "aoi-terminal",
                        "to_presence": "all",
                        "created_at": "2026-04-01",
                    }
                )
            ],
            None,
        )

        result = thought_check(mock_qdrant, my_presence="aoi-house")
        assert result["unread_count"] == 1


class TestThoughtRead:
    def test_read_marks_as_read(self, mock_qdrant, mock_embed):
        result = thought_read(mock_qdrant, thought_ids=["id-1", "id-2"])
        assert result["status"] == "read"
        assert result["marked"] == 2
        assert result["total"] == 2
        assert mock_qdrant.set_payload.call_count == 2

    def test_read_handles_missing(self, mock_qdrant, mock_embed):
        mock_qdrant.set_payload.side_effect = [Exception("not found"), None]
        result = thought_read(mock_qdrant, thought_ids=["bad-id", "good-id"])
        assert result["marked"] == 1
        assert result["total"] == 2

    def test_read_empty_list(self, mock_qdrant, mock_embed):
        result = thought_read(mock_qdrant, thought_ids=[])
        assert result["marked"] == 0
        assert result["total"] == 0


class TestThoughtHistory:
    def test_history_semantic_search(self, mock_qdrant, mock_embed):
        mock_qdrant.query_points.return_value = FakeQueryResult(
            points=[
                FakePoint(
                    payload={
                        "content": "The renders looked great",
                        "from_presence": "aoi-terminal",
                        "to_presence": "aoi-house",
                        "created_at": "2026-04-01",
                        "read": True,
                    },
                    score=0.87,
                )
            ]
        )

        result = thought_history(mock_qdrant, query="renders")
        assert len(result["thoughts"]) == 1
        assert result["thoughts"][0]["score"] == 0.87

    def test_history_with_presence_filter(self, mock_qdrant, mock_embed):
        mock_qdrant.query_points.return_value = FakeQueryResult(points=[])

        result = thought_history(
            mock_qdrant, query="test", presence_filter="aoi-terminal"
        )
        assert result["thoughts"] == []
        call_kwargs = mock_qdrant.query_points.call_args
        assert call_kwargs.kwargs.get("query_filter") is not None
