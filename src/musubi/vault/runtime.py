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
synthesis/promotion/reflection jobs, no scrape server), and the right
refactor is a future ``musubi.runtime`` module that all three
entrypoints consume. This file is the smallest change that closes
the live-reachability gap.

The TEI composite glue (``_TEICompositeEmbedder``, defined LOCALLY
in this module — NOT imported from ``musubi.vault.watcher``) is
instantiated here and wrapped in ``ChunkedEmbedder`` so sparse
inputs > 510 tokens are sliding-window-chunked + max-pooled before
they hit tei-sparse (SPLADE-v3 has a hard 512-token model cap). The
composite lives in the runtime module (NOT in the entrypoint
module) so the watcher's ``python -m musubi.vault.watcher`` never
re-loads the entrypoint via a qualified import to fetch this
class — Python executes ``python -m musubi.vault.watcher`` under
``musubi.vault.watcher.__main__``, and a qualified
``import musubi.vault.watcher`` would return the ``__main__``
module (not the regular module), creating duplicate module state.
The same adapter class is duplicated in
``src/musubi/api/bootstrap.py`` and
``src/musubi/lifecycle/runner.py``; promoting to a shared home is
the natural extraction point when a fourth caller needs it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


class _TEICompositeEmbedder:
    """Embedder Protocol impl backed by three TEI clients.

    Lives in the runtime module (NOT in :mod:`musubi.vault.watcher`)
    so the watcher's ``python -m musubi.vault.watcher`` entrypoint
    never loads the watcher module via a qualified import to fetch
    this class — that would re-enter the entrypoint module as
    ``__main__`` and create duplicate module state. The same
    adapter is duplicated in :mod:`musubi.api.bootstrap` and
    :mod:`musubi.lifecycle.runner`; promoting to a shared home is
    the natural extraction point when a fourth caller needs it.
    """

    def __init__(self, *, dense: Any, sparse: Any, reranker: Any) -> None:
        self._dense = dense
        self._sparse = sparse
        self._reranker = reranker

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        return await self._dense.embed_dense(texts)  # type: ignore[no-any-return]

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        return await self._sparse.embed_sparse(texts)  # type: ignore[no-any-return]

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        return await self._reranker.rerank(query, candidates)  # type: ignore[no-any-return]


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
        VaultRuntimeError: only when ``settings.vault_path`` is
            missing OR points at a non-directory (a regular file,
            socket, symlink to a non-directory, etc.). The watcher
            must refuse to start without a vault root;
            ``build_qdrant_client`` and friends would happily boot
            against an empty/dangling path and silently no-op.
            Dependency construction errors (Qdrant unreachable,
            TEI probe failure, coordinator / sink construction
            failure, etc.) propagate typed and AS-IS so the caller
            can decide whether to retry, restart the supervisor, or
            surface a typed failure to the systemd journal without
            a wrapped ``VaultRuntimeError`` hiding the underlying
            exception type.
    """
    if settings is None:
        settings = get_settings()
    vault_root = Path(settings.vault_path).expanduser()
    if not vault_root.exists() or not vault_root.is_dir():
        # Fail closed + visibly when ``settings.vault_path`` is
        # missing or points at a non-directory (regular file, socket,
        # etc.). The watcher must refuse to start without a vault
        # root; ``build_qdrant_client`` and friends would happily
        # boot against an empty/dangling path and silently no-op.
        if not vault_root.exists():
            raise VaultRuntimeError(
                f"vault_path={vault_root!r} does not exist; refusing to start watcher"
            )
        raise VaultRuntimeError(
            f"vault_path={vault_root!r} is not a directory "
            f"(exists but is {('a regular file' if vault_root.is_file() else 'not a directory')}); "
            "refusing to start watcher"
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
    #
    # The composite glue (``_TEICompositeEmbedder``) is defined
    # LOCALLY in this module — NOT imported from ``musubi.vault.watcher``.
    # That prevents the watcher entrypoint module from being
    # re-loaded via a qualified import when the runtime factory is
    # constructed inside the watcher's ``__main__`` block (Python
    # executes ``python -m musubi.vault.watcher`` under
    # ``musubi.vault.watcher.__main__``, and a qualified
    # ``import musubi.vault.watcher`` would return the
    # ``__main__`` module, not the regular module — duplicate
    # module state). Same adapter class lives duplicated in
    # ``src/musubi/api/bootstrap.py`` and
    # ``src/musubi/lifecycle/runner.py``.
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
