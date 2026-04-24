#!/usr/bin/env python3
"""Migrates legacy POC Qdrant data to Musubi v1 via the SDK.

Reads from localhost:6333 (the POC Qdrant container) and writes to
musubi.example.local via the Musubi v1 SDK. Supports dry-run and resumption.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ksuid import Ksuid
from pydantic import ValidationError
from qdrant_client import QdrantClient

from musubi.sdk import MusubiClient
from musubi.types.episodic import EpisodicMemory
from musubi.types.thought import Thought

# Setup basic logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Configure state tracking
STATE_FILE = Path(__file__).parent / "state.json"


def _load_state() -> dict[str, list[str]]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            logger.warning("Corrupt state.json, starting fresh.")
    return {"migrated_memories": [], "migrated_thoughts": []}


def _save_state(state: dict[str, list[str]]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _ensure_utc(dt_str: str) -> datetime:
    """Parse ISO8601 string and ensure it has a timezone."""
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _ksuid_from_uuid(uuid_str: str, epoch: float) -> str:
    """Deterministic KSUID from UUID and epoch."""
    import uuid

    try:
        u = uuid.UUID(uuid_str)
        # Use the 16 bytes of the UUID as the payload for the KSUID!
        return str(Ksuid(datetime=datetime.fromtimestamp(epoch, UTC), payload=u.bytes))
    except ValueError:
        return str(Ksuid(datetime=datetime.fromtimestamp(epoch, UTC)))


def iter_qdrant_collection(client: QdrantClient, collection_name: str) -> Iterator[dict[str, Any]]:
    """Yield all points from a Qdrant collection."""
    offset = None
    while True:
        try:
            # Check if collection exists
            client.get_collection(collection_name)
        except Exception:
            logger.warning(f"Collection {collection_name} not found.")
            break

        points, offset = client.scroll(
            collection_name=collection_name,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            yield {"id": point.id, "payload": point.payload or {}}

        if offset is None:
            break


def migrate_memories(
    source_client: QdrantClient,
    target_client: MusubiClient,
    state: dict[str, list[str]],
    dry_run: bool,
) -> tuple[int, int]:
    """Migrate musubi_memories to v1 EpisodicMemory."""
    migrated = 0
    skipped = 0

    migrated_set = set(state.get("migrated_memories", []))

    for row in iter_qdrant_collection(source_client, "musubi_memories"):
        row_id = str(row["id"])

        if row_id in migrated_set:
            continue

        payload = row["payload"]

        namespace = "eric/poc/episodic"

        created_at_str = payload.get("created_at")
        if not created_at_str:
            logger.warning(f"Skipping memory {row_id}: no created_at")
            skipped += 1
            continue

        created_epoch = payload.get("created_epoch", 1776654266.0)

        try:
            # POC: `content`, `type`, `agent`, `tags`, `context`, `created_at`, `access_count`
            new_payload = {
                "object_id": _ksuid_from_uuid(row_id, created_epoch),
                "namespace": namespace,
                "content": payload.get("content", ""),
                "state": "matured",
                "modality": "text",
                "source_context": payload.get(
                    "context", f"migrated from {payload.get('agent', 'legacy')}"
                ),
                "tags": payload.get("tags", []),
                "topics": [payload.get("type")] if payload.get("type") else [],
                "event_at": _ensure_utc(created_at_str),
                "created_at": _ensure_utc(created_at_str),
                "access_count": int(payload.get("access_count", 0)),
                "importance": 5,
            }

            # Validate via Pydantic
            EpisodicMemory.model_validate(new_payload)

            if not dry_run:
                # Preserve the source-truth timestamp by passing the
                # parsed `created_at` through. The SDK's capture method
                # supports it as an operator-only escape hatch
                # (CaptureRequest.created_at on the server); migrations
                # need operator scope anyway. Without this, the row
                # would get re-stamped at ingest time and we'd lose
                # the historical timeline from the PoC.
                target_client.episodic.capture(
                    namespace=namespace,
                    content=payload.get("content", ""),
                    tags=payload.get("tags", []),
                    topics=[payload.get("type")] if payload.get("type") else [],
                    importance=5,
                    created_at=_ensure_utc(created_at_str),
                    idempotency_key=_ksuid_from_uuid(
                        row_id, created_epoch
                    ),  # Deterministic ID mapping!
                )

            migrated += 1
            migrated_set.add(row_id)
            state["migrated_memories"] = list(migrated_set)
            if not dry_run:
                _save_state(state)

        except ValidationError as e:
            logger.warning(f"Skipping memory {row_id} due to validation error: {e}")
            skipped += 1

    return migrated, skipped


def migrate_thoughts(
    source_client: QdrantClient,
    target_client: MusubiClient,
    state: dict[str, list[str]],
    dry_run: bool,
) -> tuple[int, int]:
    """Migrate musubi_thoughts to v1 Thought."""
    migrated = 0
    skipped = 0

    migrated_set = set(state.get("migrated_thoughts", []))

    for row in iter_qdrant_collection(source_client, "musubi_thoughts"):
        row_id = str(row["id"])

        if row_id in migrated_set:
            continue

        payload = row["payload"]

        created_at_str = payload.get("created_at")
        if not created_at_str:
            skipped += 1
            continue

        created_epoch = payload.get("created_epoch", 1776654266.0)
        from_presence = payload.get("from_presence", "legacy")
        to_presence = payload.get("to_presence", "all")

        namespace = (
            f"eric/{to_presence}/thought" if to_presence != "all" else "eric/broadcast/thought"
        )

        try:
            new_payload = {
                "object_id": _ksuid_from_uuid(row_id, created_epoch),
                "namespace": namespace,
                "from_presence": from_presence,
                "to_presence": to_presence,
                "content": payload.get("content", ""),
                "channel": "default",
                "importance": 5,
                "created_at": _ensure_utc(created_at_str),
                "read": bool(payload.get("read", False)),
                "read_by": payload.get("read_by", []),
            }

            Thought.model_validate(new_payload)

            if not dry_run:
                target_client.thoughts.send(
                    namespace=namespace,
                    from_presence=from_presence,
                    to_presence=to_presence,
                    content=payload.get("content", ""),
                    channel="default",
                )

            migrated += 1
            migrated_set.add(row_id)
            state["migrated_thoughts"] = list(migrated_set)
            if not dry_run:
                _save_state(state)

        except ValidationError as e:
            logger.warning(f"Skipping thought {row_id} due to validation error: {e}")
            skipped += 1

    return migrated, skipped


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate legacy Node.js POC data to Musubi v1.",
        epilog="Reads from localhost:6333 by default and writes to Musubi v1 SDK.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate schemas and report what would be written, but write nothing.",
    )
    parser.add_argument(
        "--i-have-a-backup",
        action="store_true",
        help="Acknowledge that you have backed up the target v1 Qdrant database.",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.i_have_a_backup:
        print("ERROR: Refusing to run real migration without --i-have-a-backup.", file=sys.stderr)
        print(
            "Please backup the target Musubi v1 Qdrant instance and pass the flag.", file=sys.stderr
        )
        sys.exit(1)

    source_host = os.getenv("SOURCE_QDRANT_HOST", "127.0.0.1")
    source_port = int(os.getenv("SOURCE_QDRANT_PORT", "6333"))
    target_url = os.getenv("MUSUBI_URL", "https://musubi.example.local.example.com/v1")
    target_token = os.getenv("MUSUBI_TOKEN", "dummy")

    try:
        source_client = QdrantClient(host=source_host, port=source_port)
    except Exception as e:
        logger.error(f"Could not connect to source Qdrant at {source_host}:{source_port}: {e}")
        sys.exit(1)

    target_client = MusubiClient(base_url=target_url, token=target_token)

    state = _load_state()

    logger.info("Starting migration...")
    if args.dry_run:
        logger.info("DRY RUN MODE: No data will be written.")

    try:
        mem_migrated, mem_skipped = migrate_memories(
            source_client, target_client, state, args.dry_run
        )
        logger.info(
            f"Memories: {mem_migrated} migrated, {mem_skipped} skipped (validation failed)."
        )

        thought_migrated, thought_skipped = migrate_thoughts(
            source_client, target_client, state, args.dry_run
        )
        logger.info(
            f"Thoughts: {thought_migrated} migrated, {thought_skipped} skipped (validation failed)."
        )

    except Exception as e:
        logger.error(f"Migration aborted due to error: {e}", exc_info=True)
        sys.exit(1)

    logger.info("Migration complete.")


if __name__ == "__main__":
    main()
