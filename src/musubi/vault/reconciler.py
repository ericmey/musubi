"""Periodic drift reconciler between vault and Qdrant."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from musubi.planes.curated.plane import CuratedPlane
from musubi.types.curated import CuratedKnowledge
from musubi.vault.frontmatter import CuratedFrontmatter, parse_frontmatter

logger = logging.getLogger(__name__)


class VaultReconciler:
    """Detects and repairs drift between the filesystem and Qdrant index."""

    def __init__(self, vault_root: Path, curated_plane: CuratedPlane) -> None:
        self.vault_root = vault_root
        self.curated_plane = curated_plane

    async def reconcile(self) -> None:
        """Perform one full reconciliation pass."""
        logger.info("Starting vault reconciliation in %s", self.vault_root)

        # 1. Scan vault for new/changed files
        # use glob but be careful with case on some OSs
        vault_files = list(self.vault_root.rglob("*"))
        logger.info("Found %d total items in vault", len(vault_files))
        print(f"DEBUG: all vault items: {vault_files}")
        
        processed = 0
        for file_path in vault_files:
            if not file_path.is_file() or file_path.suffix.lower() != ".md":
                continue
            rel_parts = file_path.relative_to(self.vault_root).parts
            print(f"DEBUG: rel_parts for {file_path}: {rel_parts}")
            if any(part.startswith(".") or part.startswith("_") for part in rel_parts):
                continue
            print(f"DEBUG: calling _reconcile_file for {file_path}")
            await self._reconcile_file(file_path)
            processed += 1

        logger.info("Vault reconciliation complete. Processed %d md files", processed)

    async def _reconcile_file(self, path: Path) -> None:
        rel_path = str(path.relative_to(self.vault_root))
        print(f"DEBUG: reconciling file {rel_path}")
        try:
            content = path.read_text(encoding="utf-8")
            data, body = parse_frontmatter(content)
            print(f"DEBUG: {rel_path} has object_id: {data.get('object_id')}")
            if not data.get("object_id"):
                # Watcher will pick this up or next boot scan
                return

            body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
            fm = CuratedFrontmatter.model_validate(data)

            # Plane.create is idempotent if body_hash matches
            memory = CuratedKnowledge(
                object_id=fm.object_id,
                namespace=fm.namespace,
                vault_path=rel_path,
                body_hash=body_hash,
                title=fm.title,
                content=body,
                summary=fm.summary,
                state=fm.state,
                importance=fm.importance,
                topics=fm.topics,
                tags=fm.tags,
                version=fm.version,
                created_at=fm.created,
                updated_at=fm.updated,
            )
            await self.curated_plane.create(memory)

        except Exception as exc:
            logger.error("Failed to reconcile %s: %s", rel_path, exc)
