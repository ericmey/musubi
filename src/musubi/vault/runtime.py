"""Production runtime wiring for the vault sync watcher.

VAULT-003: the systemd unit ``deploy/systemd/musubi-vault-sync.service``
runs ``python -m musubi.vault.watcher`` and previously had no production
``VaultWatcher`` construction at all. This module centralises the
dependency graph (Qdrant, TEI, coordinator, sink, write log, settings)
into a single factory so the watcher's ``main()`` does not duplicate
a second dependency graph.

The lifecycle worker (``src/musubi/lifecycle/runner.py``) inlines a
similar graph today; the small extraction here intentionally does NOT
generalise that — the watcher needs a smaller subset (no Ollama, no
synthesis/promotion/reflection jobs, no scrape server, no
``_TEICompositeEmbedder`` glue), and the right refactor is a future
``musubi.runtime`` module that all three entrypoints consume. This
file is the smallest change that closes the live-reachability gap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from musubi.config import Settings, get_settings
from musubi.embedding.chunked import ChunkedEmbedder
from musubi.embedding.tei import TEIDenseClient, TEIRerankerClient, TEISparseClient
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.lifecycle.events import LifecycleEventSink
from musubi.planes.curated import CuratedPlane
from musubi.storage import build_qdrant_client
from musubi.vault.writelog import WriteLog

if TYPE_CHECKING:
    from musubi.embedding.base import Embedder

log = logging.getLogger(__name__)


class VaultRuntimeError(RuntimeError):
    """VAULT-003: typed error from ``build_vault_sync_runtime``."""


@dataclass(frozen=True)
class VaultSyncRuntime:
    """The production dependency bundle the watcher needs.

    Constructed by :func:`build_vault_sync_runtime` from a single
    :class:`Settings` instance. All fields are read-only; the watcher
    holds this for the lifetime of the process.
    """

    settings: Settings
    vault_root: Path
    write_log: WriteLog
    sink: LifecycleEventSink
    coordinator: LifecycleTransitionCoordinator
    curated_plane: CuratedPlane
    embedder: Embedder


def build_vault_sync_runtime(*, settings: Settings | None = None) -> VaultSyncRuntime:
    """VAULT-003: build the production wiring for the vault sync watcher.

    Mirrors the dependency graph of ``src/musubi/api/bootstrap.py``
    (Qdrant client + TEI composite + ChunkedEmbedder + curated plane +
    coordinator + sink + write log) but ONLY the fields the watcher
    needs. The coordinator and sink are constructed from the same
    settings the API server uses (same DB file, same busy timeout,
    same lease/pending cap policy) so a vault-triggered archive is
    visible to the API server's audit log and to the lifecycle
    worker.

    Args:
        settings: Production settings. ``None`` -> ``get_settings()``
            (the standard musubi-core factory).

    Raises:
        VaultRuntimeError: if any required setting is missing or the
            graph cannot be constructed.
    """
    if settings is None:
        settings = get_settings()
    vault_root = Path(settings.vault_path).expanduser()
    if not vault_root.exists():
        raise VaultRuntimeError(
            f"vault_path={vault_root!r} does not exist; refusing to start watcher"
        )

    qdrant = build_qdrant_client(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        api_key=settings.qdrant_api_key.get_secret_value(),
        https=not settings.musubi_allow_plaintext,
    )
    # Ensure the canonical collections exist (idempotent: bootstrap
    # short-circuits per collection if already present). Without this,
    # the watcher's first write hits a missing-collection Qdrant
    # error in production.
    from musubi.store import bootstrap as bootstrap_collections

    bootstrap_collections(qdrant)

    dense = TEIDenseClient(base_url=str(settings.tei_dense_url))
    sparse = TEISparseClient(base_url=str(settings.tei_sparse_url))
    reranker = TEIRerankerClient(base_url=str(settings.tei_reranker_url))
    # Wrap in ChunkedEmbedder so sparse inputs > 510 tokens are
    # sliding-window-chunked + max-pooled before they hit tei-sparse
    # (SPLADE-v3 has a hard 512-token model cap). Same wrap the API
    # server uses; without it, a long-content file delete would 413.
    # See musubi#367.
    from musubi.vault.watcher import _TEICompositeEmbedder

    embedder: Embedder = ChunkedEmbedder(
        _TEICompositeEmbedder(dense=dense, sparse=sparse, reranker=reranker)
    )
    curated_plane = CuratedPlane(client=qdrant, embedder=embedder)
    coordinator = LifecycleTransitionCoordinator(
        client=qdrant,
        db_path=settings.lifecycle_sqlite_path,
        pending_cap=settings.lifecycle_pending_cap,
        lease_ttl=settings.lifecycle_lease_ttl_s,
        backoff_base_s=settings.lifecycle_backoff_base_s,
        backoff_max_s=settings.lifecycle_backoff_max_s,
        busy_timeout_ms=settings.lifecycle_sqlite_busy_timeout_ms,
    )
    sink = LifecycleEventSink(
        db_path=settings.lifecycle_sqlite_path,
        busy_timeout_ms=settings.lifecycle_sqlite_busy_timeout_ms,
    )
    write_log_path = Path(settings.lifecycle_sqlite_path).parent / "vault-writelog.db"
    write_log = WriteLog(db_path=write_log_path)

    log.info(
        "vault-sync-runtime-built vault_root=%s qdrant=%s:%d tei_dense=%s",
        vault_root,
        settings.qdrant_host,
        settings.qdrant_port,
        settings.tei_dense_url,
    )
    return VaultSyncRuntime(
        settings=settings,
        vault_root=vault_root,
        write_log=write_log,
        sink=sink,
        coordinator=coordinator,
        curated_plane=curated_plane,
        embedder=embedder,
    )


__all__ = [
    "VaultRuntimeError",
    "VaultSyncRuntime",
    "build_vault_sync_runtime",
]
