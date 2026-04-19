"""Curated knowledge plane — Qdrant index over the vault's authoritative facts.

See [[04-data-model/curated-knowledge]] for the spec. Vault filesystem
ownership (file watcher, frontmatter parsing, write-log echo, soft-delete
to ``vault/_archive/``) lives in ``src/musubi/vault_sync/`` per
slice-vault-sync — this plane is the read/write API onto the
``musubi_curated`` Qdrant collection only.
"""

from musubi.planes.curated.plane import CuratedPlane

__all__ = ["CuratedPlane"]
