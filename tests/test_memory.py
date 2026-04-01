"""Tests for all 5 memory tools."""

from musubi.memory import (
    memory_forget,
    memory_recall,
    memory_recent,
    memory_reflect,
    memory_store,
)
from tests.conftest import FakeCollectionInfo, FakePoint, FakeQueryResult


class TestMemoryStore:
    def test_store_new_memory(self, mock_qdrant, mock_embed):
        result = memory_store(
            mock_qdrant,
            content="User prefers concise responses",
            type="feedback",
            agent="aoi",
            tags=["communication"],
        )
        assert result["status"] == "stored"
        assert "id" in result
        mock_qdrant.upsert.assert_called_once()

    def test_store_duplicate_updates(self, mock_qdrant, mock_embed):
        existing = FakePoint(
            id="existing-id",
            payload={
                "content": "Old content",
                "tags": ["old-tag"],
                "updated_at": "2026-01-01",
            },
            score=0.95,
        )
        mock_qdrant.query_points.return_value = FakeQueryResult(points=[existing])

        result = memory_store(
            mock_qdrant,
            content="Updated content",
            type="feedback",
            agent="aoi",
            tags=["new-tag"],
        )
        assert result["status"] == "updated"
        assert result["id"] == "existing-id"
        assert result["similarity"] == 0.95

    def test_store_invalid_type(self, mock_qdrant, mock_embed):
        result = memory_store(mock_qdrant, content="test", type="invalid")
        assert "error" in result

    def test_store_default_tags_none(self, mock_qdrant, mock_embed):
        result = memory_store(mock_qdrant, content="test", type="feedback")
        assert result["status"] == "stored"


class TestMemoryRecall:
    def test_recall_returns_results(self, mock_qdrant, mock_embed):
        mock_qdrant.query_points.return_value = FakeQueryResult(
            points=[
                FakePoint(
                    payload={
                        "content": "found memory",
                        "type": "feedback",
                        "agent": "aoi",
                        "tags": [],
                        "context": "",
                        "created_at": "2026-01-01",
                        "access_count": 3,
                    },
                    score=0.88,
                )
            ]
        )

        result = memory_recall(mock_qdrant, query="test query")
        assert len(result["memories"]) == 1
        assert result["memories"][0]["content"] == "found memory"
        assert result["memories"][0]["score"] == 0.88

    def test_recall_with_filters(self, mock_qdrant, mock_embed):
        mock_qdrant.query_points.return_value = FakeQueryResult(points=[])

        result = memory_recall(
            mock_qdrant,
            query="test",
            agent_filter="aoi",
            type_filter="feedback",
        )
        assert result["memories"] == []
        # Verify filter was passed
        call_kwargs = mock_qdrant.query_points.call_args
        assert call_kwargs.kwargs.get("query_filter") is not None

    def test_recall_updates_access_count(self, mock_qdrant, mock_embed):
        mock_qdrant.query_points.return_value = FakeQueryResult(
            points=[
                FakePoint(
                    id="mem-1",
                    payload={
                        "content": "test",
                        "type": "feedback",
                        "agent": "aoi",
                        "tags": [],
                        "context": "",
                        "created_at": "2026-01-01",
                        "access_count": 5,
                    },
                    score=0.85,
                )
            ]
        )

        memory_recall(mock_qdrant, query="test")
        mock_qdrant.set_payload.assert_called_once()
        payload_arg = mock_qdrant.set_payload.call_args.kwargs["payload"]
        assert payload_arg["access_count"] == 6


class TestMemoryRecent:
    def test_recent_returns_memories(self, mock_qdrant, mock_embed):
        mock_qdrant.scroll.return_value = (
            [
                FakePoint(
                    payload={
                        "content": "recent one",
                        "type": "project",
                        "agent": "aoi",
                        "tags": [],
                        "context": "",
                        "created_at": "2026-04-01T10:00:00",
                    }
                )
            ],
            None,
        )

        result = memory_recent(mock_qdrant, hours=24)
        assert len(result["memories"]) == 1
        assert result["memories"][0]["content"] == "recent one"

    def test_recent_with_filters(self, mock_qdrant, mock_embed):
        mock_qdrant.scroll.return_value = ([], None)

        result = memory_recent(mock_qdrant, hours=48, agent_filter="agent-b", type_filter="project")
        assert result["memories"] == []
        call_kwargs = mock_qdrant.scroll.call_args
        scroll_filter = call_kwargs.kwargs.get("scroll_filter")
        assert scroll_filter is not None


class TestMemoryForget:
    def test_forget_success(self, mock_qdrant, mock_embed):
        result = memory_forget(mock_qdrant, id="some-uuid")
        assert result["status"] == "forgotten"
        assert result["id"] == "some-uuid"
        mock_qdrant.delete.assert_called_once()

    def test_forget_handles_error(self, mock_qdrant, mock_embed):
        mock_qdrant.delete.side_effect = Exception("not found")
        result = memory_forget(mock_qdrant, id="bad-uuid")
        assert "error" in result


class TestMemoryReflect:
    def test_reflect_summary(self, mock_qdrant, mock_embed):
        mock_qdrant.get_collection.return_value = FakeCollectionInfo(points_count=10)
        mock_qdrant.scroll.return_value = (
            [
                FakePoint(payload={"agent": "aoi", "type": "feedback", "tags": ["rendering"]}),
                FakePoint(
                    payload={"agent": "agent-b", "type": "project", "tags": ["rendering", "lora"]}
                ),
            ],
            None,
        )

        result = memory_reflect(mock_qdrant, mode="summary")
        assert result["total_memories"] == 10
        assert result["by_agent"]["aoi"] == 1
        assert result["by_agent"]["agent-b"] == 1
        assert result["top_tags"]["rendering"] == 2

    def test_reflect_stale(self, mock_qdrant, mock_embed):
        mock_qdrant.scroll.return_value = (
            [
                FakePoint(
                    payload={
                        "content": "stale memory content here",
                        "agent": "aoi",
                        "created_at": "2026-01-01",
                        "access_count": 0,
                    }
                )
            ],
            None,
        )

        result = memory_reflect(mock_qdrant, mode="stale")
        assert len(result["stale_memories"]) == 1
        assert result["stale_memories"][0]["access_count"] == 0

    def test_reflect_frequent(self, mock_qdrant, mock_embed):
        mock_qdrant.scroll.return_value = (
            [
                FakePoint(
                    payload={
                        "content": "core memory",
                        "agent": "aoi",
                        "access_count": 50,
                        "type": "user",
                    }
                )
            ],
            None,
        )

        result = memory_reflect(mock_qdrant, mode="frequent")
        assert len(result["core_memories"]) == 1
        assert result["core_memories"][0]["access_count"] == 50

    def test_reflect_invalid_mode(self, mock_qdrant, mock_embed):
        result = memory_reflect(mock_qdrant, mode="invalid")
        assert "error" in result
