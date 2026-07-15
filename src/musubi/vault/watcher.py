"""Filesystem watcher for the Obsidian vault."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import signal
from pathlib import Path

# VAULT-003: the watcher now requires a LifecycleTransitionCoordinator.
# Imported lazily (TYPE_CHECKING) to keep the runtime import graph small
# and avoid a circular import between vault.watcher and lifecycle.coordinator.
from typing import TYPE_CHECKING, Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from musubi.planes.curated.plane import CuratedPlane
from musubi.types.common import Err, Ok, generate_ksuid, utc_now
from musubi.types.curated import CuratedKnowledge
from musubi.vault.frontmatter import CuratedFrontmatter, parse_frontmatter
from musubi.vault.namespacing import infer_namespace
from musubi.vault.writelog import WriteLog
from musubi.vault.writer import VaultWriter

if TYPE_CHECKING:
    from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator

logger = logging.getLogger(__name__)

_MAX_VAULT_MD_BYTES = 10 * 1024 * 1024
"""Oversize markdown skip threshold. 10 MB is well above any genuine
vault markdown (thousands of pages of text) while still catching
runaway-plugin rewrites or hand-pasted dumps before they flood TEI
+ Qdrant. See issue #221."""

_DEFAULT_EVENT_RATE_PER_SEC = 10.0
"""Watcher dispatch rate limit (events accepted per second, token-bucket).
Empty bucket → drop the event with a structured warning. Keeps the
event loop healthy against noisy plugins / mass renames. See issue
#219."""

_DEFAULT_INDEXING_CONCURRENCY = 10
"""Max concurrent in-flight `_handle_event` calls. Bounded via
`asyncio.Semaphore` — once this many sweeps are processing, the next
debounce fire awaits an open slot. Protects TEI + Qdrant from
parallel-request floods while giving the event loop backpressure.
See issue #219."""


class _TokenBucket:
    """Simple monotonic-clock token bucket.

    Tokens refill continuously at `rate_per_sec`. `try_consume()` takes
    one token atomically — returns True when a token was available,
    False otherwise. Not thread-safe; the Watcher only calls it from
    the event loop (via `call_soon_threadsafe`).

    Capacity is decoupled from rate: it is clamped to at least 1 token
    so sub-1/sec rates can still accumulate a token and fire — otherwise
    every event would be dropped forever.
    """

    __slots__ = ("_capacity", "_last_refill", "_rate", "_tokens")

    def __init__(self, rate_per_sec: float) -> None:
        self._rate = max(rate_per_sec, 0.0)
        self._capacity = max(self._rate, 1.0)
        self._tokens = self._capacity
        self._last_refill = 0.0

    def try_consume(self) -> bool:
        import time

        now = time.monotonic()
        if self._last_refill == 0.0:
            self._last_refill = now
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class _TEICompositeEmbedder:
    """Embedder Protocol impl backed by three TEI clients.

    Duplicated from :mod:`musubi.api.bootstrap` and
    :mod:`musubi.lifecycle.runner` deliberately — the vault watcher
    boots without touching either dependency tree, and pulling
    either into this module would break the existing layer rules.
    The class itself is 20 lines of glue; promote to a shared home
    only when a fourth caller needs it (the runtime module that
    closes the cross-process graph is the natural extraction point).
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


class WatcherHandler(FileSystemEventHandler):
    """Bridge between watchdog events and our async sync logic."""

    def __init__(self, watcher: VaultWatcher, loop: asyncio.AbstractEventLoop) -> None:
        self.watcher = watcher
        self.loop = loop

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.loop.call_soon_threadsafe(self.watcher.enqueue_event, event)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.loop.call_soon_threadsafe(self.watcher.enqueue_event, event)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.loop.call_soon_threadsafe(self.watcher.enqueue_event, event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.loop.call_soon_threadsafe(self.watcher.enqueue_event, event)


class VaultWatcher:
    """Monitors the vault and synchronizes changes to Qdrant."""

    def __init__(
        self,
        vault_root: Path,
        curated_plane: CuratedPlane,
        write_log: WriteLog,
        coordinator: LifecycleTransitionCoordinator,
        debounce_sec: float = 2.0,
        event_rate_per_sec: float = _DEFAULT_EVENT_RATE_PER_SEC,
        indexing_concurrency: int = _DEFAULT_INDEXING_CONCURRENCY,
    ) -> None:
        if event_rate_per_sec <= 0:
            raise ValueError(f"event_rate_per_sec must be > 0, got {event_rate_per_sec!r}")
        if indexing_concurrency < 1:
            raise ValueError(f"indexing_concurrency must be >= 1, got {indexing_concurrency!r}")

        self.vault_root = vault_root
        self.curated_plane = curated_plane
        self.write_log = write_log
        # VAULT-003: the canonical archive path requires the
        # LifecycleTransitionCoordinator. The required-no-default
        # parameter is the production-wiring discriminator — a
        # caller that omits the argument fails at the Python
        # call site (`TypeError: missing 1 required positional
        # argument`).
        self.coordinator = coordinator
        self.debounce_sec = debounce_sec
        self.writer = VaultWriter(vault_root, write_log)

        self._pending_tasks: dict[str, asyncio.Task[None]] = {}
        self._observer: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Rate limit + concurrency gate — see module-level docstrings
        # on the default constants.
        self._event_bucket = _TokenBucket(event_rate_per_sec)
        self._indexing_semaphore = asyncio.Semaphore(indexing_concurrency)
        self._dropped_events = 0

    def enqueue_event(self, event: FileSystemEvent) -> None:
        """Schedule processing of a file event with debouncing."""
        if not self._loop:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.error("VaultWatcher.enqueue_event called without running loop")
                return

        path = event.src_path
        if isinstance(path, bytes):
            path = path.decode("utf-8")

        # Ignore dotfiles, underscore dirs, non-md
        p = Path(path)
        try:
            rel = p.relative_to(self.vault_root)
            if any(part.startswith(".") or part.startswith("_") for part in rel.parts):
                return
        except ValueError:
            return

        if p.suffix != ".md":
            # Binary/non-markdown files aren't vault content. Emit a
            # structured warning so operators see what landed, then
            # skip processing. Deliberate: operators who want a
            # PDF/image/etc. in Musubi use /v1/artifacts/ instead of
            # drag-dropping into the vault. See issue #221.
            logger.warning(
                "vault-skip-non-markdown path=%s suffix=%s",
                str(rel),
                p.suffix or "(none)",
            )
            return

        # Rate-limit gate — drop events when the bucket is empty so
        # noisy sources (mass rename, bulk paste, plugin rewrite loops)
        # can't spawn unbounded debounce tasks. See issue #219.
        if not self._event_bucket.try_consume():
            self._dropped_events += 1
            logger.warning(
                "vault-rate-limit-drop path=%s dropped_total=%d",
                str(rel),
                self._dropped_events,
            )
            return

        def _schedule() -> None:
            # Cancel existing debounce task for this path
            if path in self._pending_tasks:
                self._pending_tasks[path].cancel()

            # Start new debounce task on the event loop
            self._pending_tasks[path] = self._loop.create_task(  # type: ignore
                self._process_after_delay(path, event)
            )

        try:
            if self._loop.is_running():
                self._loop.call_soon_threadsafe(_schedule)
            else:
                logger.warning("VaultWatcher.enqueue_event: loop is not running")
        except Exception as exc:
            logger.error("Failed to schedule debounce task: %s", exc)

    async def _process_after_delay(self, path_str: str, event: FileSystemEvent) -> None:
        try:
            await asyncio.sleep(self.debounce_sec)
            await self._handle_event(path_str, event)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Error processing event for %s: %s", path_str, exc, exc_info=True)
        finally:
            self._pending_tasks.pop(path_str, None)

    async def _handle_event(self, path_str: str, event: FileSystemEvent) -> None:
        # Bound in-flight sweeps — when the semaphore is exhausted,
        # subsequent handlers await here rather than dispatching new
        # TEI/Qdrant calls. Blocking backpressure beats dropping at
        # this layer because the debounce step already picked a
        # canonical event per path. See issue #219.
        async with self._indexing_semaphore:
            await self._handle_event_inner(path_str, event)

    async def _handle_event_inner(self, path_str: str, event: FileSystemEvent) -> None:
        logger.info("Handling event %s for %s", event.event_type, path_str)
        path = Path(path_str)
        if event.event_type == "moved" and hasattr(event, "dest_path"):
            dp = event.dest_path
            path = Path(dp.decode("utf-8") if isinstance(dp, bytes) else dp)
            path_str = str(path)

        try:
            rel_path = str(path.relative_to(self.vault_root))
        except ValueError:
            return

        if event.event_type == "deleted":
            await self._handle_deleted(rel_path)
            return

        if not path.exists():
            return

        # Size gate — a 50 MB hand-pasted dump or a runaway-plugin
        # rewrite shouldn't flood TEI + Qdrant. 10 MB is comfortably
        # above any real vault markdown (thousands of pages of text)
        # without letting a pathological case through. Operators who
        # need a large blob indexed route it through /v1/artifacts/
        # rather than dropping it into the vault. See issue #221.
        try:
            stat_result = path.stat()
        except OSError as exc:
            logger.error("Failed to stat file %s: %s", path, exc)
            return
        if stat_result.st_size > _MAX_VAULT_MD_BYTES:
            logger.warning(
                "vault-skip-oversize-markdown path=%s bytes=%d limit=%d",
                rel_path,
                stat_result.st_size,
                _MAX_VAULT_MD_BYTES,
            )
            return

        # Read file
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.error("Failed to read file %s: %s", path, exc)
            return

        data, body = parse_frontmatter(content)
        logger.info("Parsed frontmatter: %s", data)
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

        # Echo prevention
        if self.write_log.consume_if_exists(rel_path, body_hash):
            logger.debug("Echo prevented for %s", rel_path)
            return

        # Validation
        try:
            if not data.get("object_id"):
                # Bootstrap ID
                await self._bootstrap_id(rel_path, data, body)
                return

            fm = CuratedFrontmatter.model_validate(data)
        except Exception as exc:
            logger.error("Frontmatter validation failed for %s: %s", rel_path, exc)
            # TODO: Emit Thought to ops-alerts
            return

        # Index
        if not fm.object_id or not fm.namespace:
            logger.error("Missing object_id or namespace for %s after validation", rel_path)
            return

        from typing import Literal, cast

        memory = CuratedKnowledge(
            object_id=fm.object_id,
            namespace=fm.namespace,
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

    async def _bootstrap_id(self, rel_path: str, data: dict[str, Any], body: str) -> None:
        """Generate object_id and write back to file."""
        now = utc_now()
        data.update(
            {
                "object_id": generate_ksuid(),
                "created": now,
                "updated": now,
                "namespace": infer_namespace(rel_path),
            }
        )
        # Ensure it has a title if we're writing it back
        if not data.get("title"):
            data["title"] = Path(rel_path).stem

        fm = CuratedFrontmatter.model_validate(data)
        self.writer.write_curated(rel_path, fm, body)
        logger.info("Bootstrapped object_id for %s", rel_path)

    async def _handle_deleted(self, rel_path: str) -> None:
        """VAULT-003: archive the curated row matching ``rel_path`` through the
        canonical ``LifecycleTransitionCoordinator`` seam.

        The file is gone, so we do NOT re-read the frontmatter. Identity
        comes from the STORED ``vault_path`` on the existing row
        (exact match via :meth:`CuratedPlane.find_by_vault_path`).

        Outcomes:
          - ``Err(not_found)`` from find_by_vault_path -> log ``info``
            (clean observable no-op) and return.
          - ``Err(multiple_matches)`` -> log structured ``warning`` with
            the conflicting object_ids and refuse to archive (fail closed,
            visibly). A manual operator must reconcile the duplicate.
          - ``Ok(current)`` and ``current.state == 'archived'`` -> log
            ``debug`` (idempotent no-op repeat delete; we do NOT re-issue
            the transition).
          - ``Ok(current)`` and ``current.state != 'archived'`` -> run
            the canonical transition to ``archived`` via the coordinator.
          - ``Ok(TransitionResult)`` -> log ``info`` (archive committed).
          - ``Err(TransitionError(code='illegal_transition'))`` -> log
            structured ``warning`` with the actual ``from_state``; the
            watcher's pre-read state check above already filtered the
            common repeat-delete case, so this is a real anomaly
            (superseded/demoted/etc row hitting archive) and stays
            visible. NO IN-HANDLER RETRY.
        """
        lookup = await self.curated_plane.find_by_vault_path(rel_path)
        if isinstance(lookup, Err):
            if lookup.error.code == "multiple_matches":
                logger.warning(
                    "vault-delete-failed-multiple-matches path=%s match_count=%d match_object_ids=%s",
                    rel_path,
                    lookup.error.match_count,
                    ",".join(lookup.error.match_object_ids),
                )
                return
            # not_found or any other error code: clean observable no-op.
            logger.info(
                "vault-delete-noop path=%s reason=%s",
                rel_path,
                lookup.error.code,
            )
            return

        current = lookup.value
        if current.state == "archived":
            # Already archived (repeat delete on the same row) —
            # idempotent no-op. We do NOT re-issue the transition
            # because the canonical state machine would surface
            # `illegal_transition(archived -> archived)` and we don't
            # want a successful-looking log on a no-op. Wrong-state
            # rows (superseded/demoted/etc) fall through to the
            # transition and surface their illegal_transition as a
            # visible warning.
            logger.debug(
                "vault-delete-idempotent path=%s object_id=%s state=archived",
                rel_path,
                current.object_id,
            )
            return

        result = await self.curated_plane.transition(
            namespace=current.namespace,
            object_id=current.object_id,
            to_state="archived",
            actor="vault-watcher",
            reason=f"vault file deleted: {rel_path}",
            coordinator=self.coordinator,
        )

        if isinstance(result, Ok):
            logger.info(
                "vault-delete-archived path=%s object_id=%s outcome=ok",
                rel_path,
                current.object_id,
            )
            return

        # Err(TransitionError) — every code here is a real anomaly now
        # that we've pre-filtered the common repeat-delete case. Log
        # the from_state so operators can see WHY it failed (e.g.
        # superseded -> archived is illegal in the curated state table).
        err = result.error
        logger.warning(
            "vault-delete-failed path=%s object_id=%s code=%s from_state=%s to_state=%s message=%s",
            rel_path,
            current.object_id,
            err.code,
            getattr(err, "from_state", "unknown"),
            getattr(err, "to_state", "unknown"),
            err.message,
        )

    def boot_scan(self) -> None:
        """Run a background scan over the vault to catch missed edits."""
        if not self._loop:
            logger.warning("VaultWatcher.boot_scan called without a running loop")
            return

        async def _scan_task() -> None:
            logger.info("Starting vault boot scan")
            try:
                known_hashes = {}
                offset = None
                while True:
                    resp = self.curated_plane._client.scroll(
                        collection_name="musubi_curated",
                        limit=1000,
                        offset=offset,
                        with_payload=["vault_path", "body_hash"],
                        with_vectors=False,
                    )
                    points, offset = resp[0], resp[1]
                    for pt in points:
                        if pt.payload:
                            vp = pt.payload.get("vault_path")
                            bh = pt.payload.get("body_hash")
                            if vp and bh:
                                known_hashes[vp] = bh
                    if offset is None:
                        break
            except Exception as exc:
                logger.error("Failed to fetch curated paths for boot scan: %s", exc)
                return

            import hashlib

            from watchdog.events import FileSystemEvent

            from musubi.vault.frontmatter import parse_frontmatter

            for path in self.vault_root.rglob("*.md"):
                try:
                    rel_path = path.relative_to(self.vault_root)
                    if any(part.startswith(".") or part.startswith("_") for part in rel_path.parts):
                        continue

                    rel_str = str(rel_path)
                    content = path.read_text(encoding="utf-8")
                    _, body = parse_frontmatter(content)
                    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

                    if known_hashes.get(rel_str) != body_hash:
                        logger.info("Boot scan found drift in %s", rel_str)
                        evt = FileSystemEvent(str(path))
                        evt.event_type = "modified"
                        # VAULT-002 fix: pass the ABSOLUTE path
                        # to _handle_event so the inner
                        # `path.relative_to(self.vault_root)`
                        # succeeds. Previously we passed the
                        # relative path string, which raised
                        # ValueError inside _handle_event_inner
                        # (line ~230) and was silently swallowed
                        # by the `except ValueError: return` —
                        # the file was never processed.
                        await self._handle_event(str(path), evt)
                except Exception as exc:
                    logger.error("Boot scan failed on path %s: %s", path, exc)

            logger.info("Boot scan complete")

        self._loop.create_task(_scan_task())

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if not loop:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # If no loop in current thread, we might be starting from a main thread
                # but Watcher expects to work with a running loop for its tasks.
                raise RuntimeError(
                    "VaultWatcher.start() must be called from a thread with a running loop, or provided a loop."
                ) from None
        self._loop = loop
        self._observer = Observer()
        handler = WatcherHandler(self, loop)
        self._observer.schedule(handler, str(self.vault_root), recursive=True)
        self._observer.start()
        logger.info("Vault watcher started for %s", self.vault_root)

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        logger.info("Vault watcher stopped")


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


async def _main_async() -> None:
    """VAULT-003: the production entrypoint invoked by
    ``python -m musubi.vault.watcher`` (per
    ``deploy/systemd/musubi-vault-sync.service``).

    Builds the canonical runtime, constructs the watcher, runs the
    one-time boot scan, starts the watchdog observer, and stays
    alive until SIGTERM/SIGINT. On exit, the observer is stopped
    cleanly and the runtime's sink is closed. ``WriteLog`` has no
    ``close()`` — it opens sqlite connections per call, so each
    ``record_write`` / ``consume_if_exists`` is its own transaction;
    there is no per-process connection to release.
    """
    from musubi.observability import configure_logging
    from musubi.vault.runtime import build_vault_sync_runtime

    configure_logging()
    runtime = build_vault_sync_runtime()
    watcher = VaultWatcher(
        vault_root=runtime.vault_root,
        curated_plane=runtime.curated_plane,
        write_log=runtime.write_log,
        coordinator=runtime.coordinator,
    )
    loop = asyncio.get_running_loop()
    watcher.start(loop=loop)
    # Run the boot scan after the observer is up; the scan dispatches
    # via the same _handle_event path so it respects the canonical
    # coordinator + curated plane wiring.
    watcher.boot_scan()

    stop_event = asyncio.Event()

    def _request_stop(*_args: object) -> None:
        logger.info("vault-watcher-signal-received; requesting stop")
        stop_event.set()

    import contextlib

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, ValueError):
            # Windows / non-unix / signal already installed; skip.
            loop.add_signal_handler(sig, _request_stop)

    try:
        await stop_event.wait()
    finally:
        watcher.stop()
        runtime.sink.close()
        logger.info("vault-watcher-exited-cleanly")


def main() -> None:
    """Synchronous entrypoint for ``python -m musubi.vault.watcher``."""
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()


__all__ = [
    "VaultWatcher",
    "WatcherHandler",
    "main",
]
