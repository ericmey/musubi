"""Tests for ``ensure_indexes``.

Payload indexes are no-ops in qdrant-client's local mode, so we verify the
*call shape* with a MagicMock client, and verify idempotent-skip behavior
when the server reports an existing schema.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from musubi.store import ensure_indexes
from musubi.store.specs import UNIVERSAL_INDEXES, all_indexes_for


class TestIndexesCreatedOnVirginNode:
    def test_every_declared_index_is_created_on_first_boot(self, mock_client: MagicMock) -> None:
        created = ensure_indexes(mock_client)
        assert set(created.keys()) == {
            "musubi_episodic",
            "musubi_curated",
            "musubi_concept",
            "musubi_artifact",
            "musubi_artifact_chunks",
            "musubi_thought",
            "musubi_lifecycle_events",
        }

    def test_universal_fields_included_for_every_collection(self, mock_client: MagicMock) -> None:
        created = ensure_indexes(mock_client)
        universal = {spec.field_name for spec in UNIVERSAL_INDEXES}
        for coll, fields in created.items():
            assert universal.issubset(set(fields)), (
                f"{coll} missing universal fields: {universal - set(fields)}"
            )

    def test_namespace_in_every_collection(self, mock_client: MagicMock) -> None:
        created = ensure_indexes(mock_client)
        for coll, fields in created.items():
            assert "namespace" in fields, f"{coll} missing namespace index"

    def test_total_create_calls_matches_spec(self, mock_client: MagicMock) -> None:
        from musubi.store import COLLECTION_NAMES

        ensure_indexes(mock_client)
        expected = sum(len(all_indexes_for(coll)) for coll in COLLECTION_NAMES)
        assert mock_client.create_payload_index.call_count == expected

    def test_passes_correct_field_schema_enum(self, mock_client: MagicMock) -> None:
        from qdrant_client import models

        ensure_indexes(mock_client, only="musubi_episodic")
        # Check at least one call passed the KEYWORD enum for the namespace field.
        namespace_calls = [
            c
            for c in mock_client.create_payload_index.call_args_list
            if c.kwargs.get("field_name") == "namespace"
        ]
        assert len(namespace_calls) == 1
        assert namespace_calls[0].kwargs["field_schema"] == (models.PayloadSchemaType.KEYWORD)


class TestIdempotency:
    def test_second_boot_is_no_op_when_server_reports_existing_schema(
        self, mock_client: MagicMock
    ) -> None:
        from musubi.store.specs import all_indexes_for

        # First boot: server reports no existing schema; all indexes created.
        first = ensure_indexes(mock_client, only="musubi_episodic")
        assert len(first["musubi_episodic"]) == len(all_indexes_for("musubi_episodic"))

        # Now simulate the server reporting those fields back.
        fields = {spec.field_name for spec in all_indexes_for("musubi_episodic")}
        mock_client.get_collection.return_value = MagicMock(
            payload_schema={f: MagicMock() for f in fields}
        )
        # Reset the call counter so we can assert zero new creates this round.
        mock_client.create_payload_index.reset_mock()

        second = ensure_indexes(mock_client, only="musubi_episodic")
        assert second["musubi_episodic"] == []
        mock_client.create_payload_index.assert_not_called()

    def test_already_exists_exception_treated_as_noop(self) -> None:
        client = MagicMock()
        client.get_collections.return_value = MagicMock(collections=[])
        client.get_collection.return_value = MagicMock(payload_schema={})
        client.create_payload_index.side_effect = RuntimeError(
            "Index for field 'namespace' already exists"
        )

        created = ensure_indexes(client, only="musubi_episodic")
        # Every call raised "already exists" → nothing counted as newly created.
        assert created["musubi_episodic"] == []

    def test_other_exceptions_bubble_up(self) -> None:
        import pytest

        client = MagicMock()
        client.get_collections.return_value = MagicMock(collections=[])
        client.get_collection.return_value = MagicMock(payload_schema={})
        client.create_payload_index.side_effect = RuntimeError(
            "qdrant blew up for unrelated reasons"
        )

        with pytest.raises(RuntimeError, match="blew up"):
            ensure_indexes(client, only="musubi_episodic")


class TestOnlyFilter:
    def test_only_single_collection(self, mock_client: MagicMock) -> None:
        created = ensure_indexes(mock_client, only="musubi_thought")
        assert set(created.keys()) == {"musubi_thought"}
