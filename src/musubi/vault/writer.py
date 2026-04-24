"""Atomic writer for the Obsidian vault."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from musubi.vault.frontmatter import CuratedFrontmatter, dump_frontmatter
from musubi.vault.writelog import WriteLog

logger = logging.getLogger(__name__)


class VaultWriter:
    """Handles all writes to the Obsidian vault from Core."""

    def __init__(self, vault_root: Path, write_log: WriteLog) -> None:
        self.vault_root = vault_root
        self.write_log = write_log

    def write_curated(
        self,
        vault_relative_path: str,
        frontmatter: CuratedFrontmatter,
        body: str,
    ) -> Path:
        """Write a curated markdown file to the vault.

        Updates the write-log beforehand to ensure the watcher ignores this event.
        """
        # Defense-in-depth: reject any path that escapes `vault_root`.
        # The promotion sweep sanitizes topics with `slugify` before
        # composing the path, but anything that wires a new caller to
        # `write_curated` could forget. Resolve both sides and verify
        # the target sits under vault_root.
        full_path = (self.vault_root / vault_relative_path.lstrip("/")).resolve()
        vault_root_resolved = self.vault_root.resolve()
        if vault_root_resolved not in full_path.parents and full_path != vault_root_resolved:
            raise ValueError(
                f"vault-path-escape: {vault_relative_path!r} resolves outside "
                f"vault_root {vault_root_resolved}"
            )
        full_path.parent.mkdir(parents=True, exist_ok=True)

        # Normalize body and compute hash
        body_normalized = body.lstrip()
        body_bytes = body_normalized.encode("utf-8")
        body_hash = hashlib.sha256(body_bytes).hexdigest()

        # Update frontmatter meta (Musubi is writing this)
        # Note: we use model_dump(by_alias=True) to get "musubi-managed" correctly
        fm_data = frontmatter.model_dump(by_alias=True, mode="json")

        # Record in write-log BEFORE writing to disk
        self.write_log.record_write(vault_relative_path, body_hash)

        # Serialize
        content = dump_frontmatter(fm_data, body_normalized)

        # Atomic write
        temp_path = full_path.with_suffix(".tmp")
        try:
            temp_path.write_text(content, encoding="utf-8")
            temp_path.replace(full_path)
        except Exception as exc:
            logger.error("Failed to write to %s: %s", full_path, exc, exc_info=True)
            if temp_path.exists():
                temp_path.unlink()
            raise

        return full_path
