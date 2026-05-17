#!/usr/bin/env python3
"""One-shot: force a synthesis sweep for all (or one) identity families.

The lifecycle worker only runs the synthesis sweep once a day at 03:00 UTC.
This script bypasses the cron and invokes `synthesis_run` directly for
every discovered identity family, using the same code path the worker
would take. Useful for:

- Verifying a fresh deploy actually produces concepts (don't wait 24h)
- Re-running synthesis after a cursor reset (see also: scripts/backfill_identity_family.py)
- Debugging "why didn't synthesis create concepts for family X" without
  waiting for the daily tick

USAGE (from the musubi host, inside the core container):

    sudo docker cp scripts/force_synthesis.py musubi-core-1:/tmp/
    sudo docker exec -e MUSUBI_FORCE_SYNTHESIS_CONFIRM=1 musubi-core-1 \\
        python3 /tmp/force_synthesis.py

WITHOUT the confirm env var, the script lists families that WOULD be
processed and exits (dry run). With the confirm var set, runs synthesis
for each family and prints a report.

To target a single family:

    sudo docker exec -e MUSUBI_FORCE_SYNTHESIS_CONFIRM=1 \\
        -e MUSUBI_FORCE_FAMILY=aoi musubi-core-1 \\
        python3 /tmp/force_synthesis.py

Safe to run repeatedly: synthesis is idempotent given the cursor +
candidates pool. Calling it twice in a row produces zero new clusters
on the second run because the first run already removed clustered
memories and upserted unclustered ones as candidates.
"""

from __future__ import annotations

import asyncio
import os
import sys


def main() -> int:
    from musubi.config import get_settings
    from musubi.embedding.tei import TEIDenseClient, TEIRerankerClient, TEISparseClient
    from musubi.lifecycle.events import LifecycleEventSink
    from musubi.lifecycle.maturation import default_ollama_client
    from musubi.lifecycle.synthesis import (
        SynthesisCursor,
        _discover_identity_families,
        synthesis_run,
    )
    from musubi.storage import build_qdrant_client

    # Build the same composite embedder the runner uses.
    class _Composite:
        def __init__(self, dense, sparse, reranker):
            self.dense = dense
            self.sparse = sparse
            self.reranker = reranker

        async def embed_dense(self, texts):
            return await self.dense.embed_dense(texts)

        async def embed_sparse(self, texts):
            return await self.sparse.embed_sparse(texts)

        async def rerank(self, query, candidates):
            return await self.reranker.rerank(query, candidates)

    settings = get_settings()
    qdrant = build_qdrant_client(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        api_key=settings.qdrant_api_key.get_secret_value(),
        https=not settings.musubi_allow_plaintext,
    )
    sink = LifecycleEventSink(db_path=settings.lifecycle_sqlite_path)
    cursor = SynthesisCursor(db_path=settings.lifecycle_sqlite_path)
    ollama = default_ollama_client()
    embedder = _Composite(
        dense=TEIDenseClient(base_url=str(settings.tei_dense_url)),
        sparse=TEISparseClient(base_url=str(settings.tei_sparse_url)),
        reranker=TEIRerankerClient(base_url=str(settings.tei_reranker_url)),
    )

    families = _discover_identity_families(qdrant)
    target_family = os.environ.get("MUSUBI_FORCE_FAMILY", "").strip()
    if target_family:
        if target_family not in families:
            print(
                f"FATAL: family {target_family!r} not found. Discovered: {families}",
                file=sys.stderr,
            )
            return 2
        families = [target_family]

    confirm = os.environ.get("MUSUBI_FORCE_SYNTHESIS_CONFIRM", "") == "1"

    print(f"=== Force synthesis ({'EXECUTING' if confirm else 'DRY RUN'}) ===")
    print(f"Families to process: {families}")
    print()

    if not confirm:
        print("DRY RUN — set MUSUBI_FORCE_SYNTHESIS_CONFIRM=1 to execute.")
        return 0

    async def _run_all():
        for family in families:
            print(f"[{family}]", flush=True)
            try:
                report = await synthesis_run(
                    client=qdrant,
                    sink=sink,
                    ollama=ollama,  # type: ignore[arg-type]
                    embedder=embedder,  # type: ignore[arg-type]
                    cursor=cursor,
                    namespace=family,
                )
                print(
                    f"  selected={report.memories_selected} "
                    f"clusters={report.clusters_formed} "
                    f"created={report.concepts_created} "
                    f"reinforced={report.concepts_reinforced} "
                    f"contradictions={report.contradictions_detected} "
                    f"candidates_carried={report.candidates_carried_forward} "
                    f"pruned={report.candidates_pruned}",
                    flush=True,
                )
            except Exception as exc:
                print(f"  FAILED: {type(exc).__name__}: {exc}", flush=True)

    asyncio.run(_run_all())
    return 0


if __name__ == "__main__":
    sys.exit(main())
