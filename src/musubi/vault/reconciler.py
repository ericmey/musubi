"""Periodic drift reconciler between vault and Qdrant.

The lifecycle-worker's ``vault_reconcile`` job calls :meth:`VaultReconciler.reconcile`
every 6 hours. It walks the vault filesystem, parses frontmatter, and
upserts any markdown file with a non-empty ``object_id`` into the
curated plane.

Scope of this slice (musubi#345, partial):

- Clean up debug-print code, add structured logging at INFO/DEBUG.
- Skip-on-unchanged: body-hash compare so unchanged files don't churn
  embeddings on every 6h tick.
- Wire into the lifecycle scheduler so the job runs.

Out of scope (separate session per the issue):

- ``musubi-vault-watcher`` real-time process — needs an architecture
  decision on where the vault lives relative to the musubi host
  (mounted, NAS-shared, git-synced).
- Deletion handling — currently the reconciler is upsert-only; vault
  files that disappear leave their qdrant rows behind. Requires a
  "what's in qdrant but not on disk" pass + a deletion contract.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Literal, cast

from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator, TransitionPending
from musubi.planes.curated.plane import CuratedPlane
from musubi.types.common import Err
from musubi.types.curated import CuratedKnowledge
from musubi.vault.frontmatter import CuratedFrontmatter, parse_frontmatter
from musubi.vault.watcher import infer_namespace

logger = logging.getLogger(__name__)


class VaultReconciler:
    """Detects and repairs drift between the filesystem and Qdrant index.

    Idempotent and skip-on-unchanged: files whose body-hash matches the
    last reconciled state are not re-upserted. Files without
    ``object_id`` frontmatter are skipped (the watcher slice — when
    implemented — generates IDs at first save; the reconciler only
    deals with already-stamped files).
    """

    def __init__(
        self,
        vault_root: Path,
        curated_plane: CuratedPlane,
        coordinator: LifecycleTransitionCoordinator,
    ) -> None:
        self.vault_root = vault_root
        self.curated_plane = curated_plane
        self.coordinator = coordinator
        # In-memory body-hash cache: ``object_id`` → last-upserted hash.
        # Survives the process lifetime; on restart the first pass
        # re-upserts everything (one-time cost; subsequent passes are
        # quiet). Bigger persistence would need a SQLite table — out of
        # scope for this slice; the in-memory cache is enough to avoid
        # the steady-state churn complaint.
        self._last_seen_hash: dict[str, str] = {}

    async def reconcile(self) -> int:
        """Perform one full reconciliation pass.

        Returns the number of files actually upserted (not the total
        scanned). The lifecycle scheduler doesn't use the return value;
        it's exposed for tests + future operator endpoints.
        """
        logger.info("vault-reconcile starting in %s", self.vault_root)

        if not self.vault_root.exists():
            logger.warning("vault-reconcile root does not exist: %s", self.vault_root)
            return 0

        scanned = 0
        upserted = 0
        skipped_unchanged = 0
        skipped_no_id = 0
        errored = 0

        seen_paths: set[str] = set()
        for file_path in self.vault_root.rglob("*"):
            if not file_path.is_file() or file_path.suffix.lower() != ".md":
                continue
            try:
                rel_parts = file_path.relative_to(self.vault_root).parts
            except ValueError:
                continue
            # Hidden dirs (`.obsidian`, `.git`), Markdown-Obsidian
            # scratch dirs (`_sketch`, `_secrets`), and similar.
            if any(p.startswith(".") or p.startswith("_") for p in rel_parts):
                continue

            scanned += 1
            rel_str = str(file_path.relative_to(self.vault_root))
            seen_paths.add(rel_str)
            outcome = await self._reconcile_file(file_path)
            if outcome == "upserted":
                upserted += 1
            elif outcome == "unchanged":
                skipped_unchanged += 1
            elif outcome == "no_object_id":
                skipped_no_id += 1
            elif outcome == "error":
                errored += 1

        # Ghost row reconciliation (VAULT-001)
        ghosts_archived = 0
        ghosts_pending = 0
        try:
            inventory = await self.curated_plane.scan_vault_rows()
            for row in inventory:
                vp = row.vault_path
                if not vp or row.state in ("archived", "superseded"):
                    continue
                if vp not in seen_paths:
                    expected_ns = infer_namespace(vp)
                    if row.namespace != expected_ns:
                        logger.debug(
                            "Ghost row candidate %s namespace %s does not match expected %s, skipping",
                            vp,
                            row.namespace,
                            expected_ns,
                        )
                        continue

                    try:
                        res = await self.curated_plane.transition(
                            namespace=row.namespace,
                            object_id=row.object_id,
                            to_state="archived",
                            actor="system/vault-reconciler",
                            reason=f"Ghost row reconciliation (deleted from disk): {vp}",
                            coordinator=self.coordinator,
                        )
                        if isinstance(res, Err):
                            logger.error(
                                "Failed to archive ghost row %s: %s", vp, res.error.message
                            )
                            errored += 1
                        else:
                            if isinstance(res.value, TransitionPending):
                                logger.info("Ghost row archive pending for %s", vp)
                                ghosts_pending += 1
                            else:
                                logger.info("Archived ghost row missing from disk: %s", vp)
                                ghosts_archived += 1
                    except Exception as exc:
                        logger.error("Error archiving ghost row %s: %s", vp, exc)
                        errored += 1
        except Exception as exc:
            logger.error("Failed to scan curated plane for ghost rows: %s", exc)

        logger.info(
            "vault-reconcile complete scanned=%d upserted=%d "
            "unchanged=%d no_object_id=%d errored=%d ghosts_archived=%d ghosts_pending=%d",
            scanned,
            upserted,
            skipped_unchanged,
            skipped_no_id,
            errored,
            ghosts_archived,
            ghosts_pending,
        )
        return upserted

    async def _reconcile_file(
        self, path: Path
    ) -> Literal["upserted", "unchanged", "no_object_id", "error"]:
        rel_path = str(path.relative_to(self.vault_root))
        try:
            content = path.read_text(encoding="utf-8")
            data, body = parse_frontmatter(content)
            object_id = data.get("object_id")
            if not object_id:
                # Watcher will pick this up at first save; the reconciler
                # only deals with already-stamped files.
                return "no_object_id"

            body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

            # Skip-on-unchanged: if we've already upserted this file at
            # the current body-hash, no work to do. Saves an embedding
            # call + qdrant write on every 6h tick for the steady-state
            # case (vault is mostly read-only between writes).
            previous_hash = self._last_seen_hash.get(str(object_id))
            if previous_hash == body_hash:
                return "unchanged"

            fm = CuratedFrontmatter.model_validate(data)
            memory = CuratedKnowledge(
                object_id=fm.object_id,  # type: ignore[arg-type]
                namespace=fm.namespace,  # type: ignore[arg-type]
                vault_path=rel_path,
                body_hash=body_hash,
                title=fm.title,
                content=body,
                summary=fm.summary,
                state=cast(Literal["matured", "superseded", "archived"], fm.state),
                importance=fm.importance,
                topics=fm.topics,
                tags=fm.tags,
                version=fm.version,
                created_at=fm.created,
                updated_at=fm.updated,
            )
            await self.curated_plane.create(memory)
            self._last_seen_hash[str(object_id)] = body_hash
            logger.debug("vault-reconcile upserted %s (hash=%s)", rel_path, body_hash[:12])
            return "upserted"

        except Exception as exc:
            logger.error("vault-reconcile failed for %s: %s", rel_path, exc)
            return "error"


def build_vault_reconcile_jobs(
    *,
    vault_root: Path,
    curated_plane: CuratedPlane,
    lock_dir: Path,
    coordinator: LifecycleTransitionCoordinator,
) -> list[Any]:
    """Return the one-element ``Job`` list for the lifecycle scheduler.

    Mirrors :func:`musubi.lifecycle.scheduler.build_default_jobs`'s
    ``vault_reconcile`` entry (interval=6h). File lock at
    ``lock_dir/vault_reconcile.lock`` serialises against any other
    worker attempting the same pass.

    Replaces the placeholder ``_placeholder("vault_reconcile")`` that
    logged "not yet implemented; skipping" on every tick before this
    slice landed.
    """
    import asyncio as _asyncio

    from musubi.lifecycle.scheduler import Job, file_lock

    lock_path = lock_dir / "vault_reconcile.lock"
    reconciler = VaultReconciler(
        vault_root=vault_root, curated_plane=curated_plane, coordinator=coordinator
    )

    async def _run_once() -> None:
        try:
            upserted = await reconciler.reconcile()
            logger.info("vault-reconcile-job done upserted=%d", upserted)
        except Exception:
            logger.exception("vault-reconcile-job failed")

    def _runner() -> None:
        with file_lock(lock_path) as acquired:
            if not acquired:
                logger.info("lifecycle-job=vault_reconcile lock-held; skipping run")
                return
            _asyncio.run(_run_once())

    return [
        Job(
            name="vault_reconcile",
            trigger_kind="interval",
            trigger_kwargs={"hours": 6},
            func=_runner,
            grace_time_s=1800,
        ),
    ]


__all__ = ["VaultReconciler", "build_vault_reconcile_jobs"]
