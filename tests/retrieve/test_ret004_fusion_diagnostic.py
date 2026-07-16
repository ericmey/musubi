"""RET-004 fusion diagnostic — Yua-approved single read-only instrumented dispatch (2026-07-15).

Captures dense-only / sparse-only / current-fused per-query ranked lists (object_id + score) for
the FROZEN 16-group BEIR corpus and the FROZEN scheduled corpus, so weighted-fusion behavior can be
reconstructed and simulated OFFLINE from the CI log. This test:

  * changes NO ranking code, NO corpus, NO thresholds — it only reads/measures;
  * uses the same production seams the real gates use (EpisodicPlane.create, canonical maturation,
    hybrid_search) with dense-only (sparse_weight=0) and sparse-only (dense_weight=0) toggles;
  * seeds into RUN-UNIQUE namespaces and tears them down in a best-effort finally;
  * is ``integration``-marked, so it is deselected locally and executed only by the x86 TEI CI.

It emits one JSON blob delimited by RET004-DIAG-JSON markers to stdout; the offline analysis reads it
from the CI log. It does not assert a threshold — it is a capture harness, not a gate.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from musubi.types.common import Err

# Capture depth per channel: enough to reconstruct the top-10 fused order at any weight without
# truncating a candidate that could rise into the top-10 when re-weighted.
DIAG_LIMIT = 25


def _ranked(
    client: Any,
    embedder: Any,
    *,
    namespace: str,
    query: str,
    collection: str,
    state_filter: Any,
    dense_w: float,
    sparse_w: float,
) -> list[dict[str, Any]]:
    """One hybrid_search, returning [{id, score}] in rank order. dense_w/sparse_w select the channel
    (0.0 disables it — the existing on/off semantics), never a magnitude."""
    from musubi.retrieve.hybrid import hybrid_search

    result = asyncio.run(
        hybrid_search(
            client,
            embedder,
            namespace=namespace,
            query=query,
            collection=collection,
            limit=DIAG_LIMIT,
            state_filter=state_filter,
            dense_weight=dense_w,
            sparse_weight=sparse_w,
            timeout_s=30.0,
            sparse_timeout_s=30.0,
        )
    )
    if isinstance(result, Err):
        raise AssertionError(
            f"hybrid_search failed (dense_w={dense_w}, sparse_w={sparse_w}): {result}"
        )
    return [{"id": hit.object_id, "score": round(float(hit.score), 6)} for hit in result.value.hits]


def _capture_three(
    client: Any, embedder: Any, *, namespace: str, query: str, collection: str, state_filter: Any
) -> dict[str, Any]:
    """dense-only, sparse-only, and current fused (RRF) ranked lists for one query."""
    return {
        "dense": _ranked(
            client,
            embedder,
            namespace=namespace,
            query=query,
            collection=collection,
            state_filter=state_filter,
            dense_w=1.0,
            sparse_w=0.0,
        ),
        "sparse": _ranked(
            client,
            embedder,
            namespace=namespace,
            query=query,
            collection=collection,
            state_filter=state_filter,
            dense_w=0.0,
            sparse_w=1.0,
        ),
        "fused": _ranked(
            client,
            embedder,
            namespace=namespace,
            query=query,
            collection=collection,
            state_filter=state_filter,
            dense_w=1.0,
            sparse_w=1.0,
        ),
    }


def _teardown_namespace(client: Any, collection: str, namespace: str) -> None:
    """Best-effort delete of every point in ``namespace`` (diagnostic isolation)."""
    from qdrant_client import models

    try:
        client.delete(
            collection_name=collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="namespace", match=models.MatchValue(value=namespace)
                        )
                    ]
                )
            ),
        )
    except Exception as exc:
        print(f"RET004-DIAG teardown warning for {namespace!r}: {exc!r}")


@pytest.mark.integration
def test_ret004_fusion_diagnostic_capture() -> None:
    from tests.retrieve.test_hybrid import _beir_query_groups

    from musubi.evals.live_gate import build_settings_backends
    from musubi.evals.scheduled_gate import (
        _seed_documents,
        load_corpus,
        new_run_id,
        run_namespace,
        wait_for_visibility,
    )
    from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
    from musubi.planes.episodic.plane import EpisodicPlane
    from musubi.store.names import collection_for_plane
    from musubi.types.episodic import EpisodicMemory

    backends = build_settings_backends()  # raises without the real stack (never faked)
    client, embedder = backends.client, backends.embedder
    collection = collection_for_plane("episodic")
    plane = EpisodicPlane(client=client, embedder=embedder)
    coordinator = LifecycleTransitionCoordinator(
        client=client, db_path=Path(tempfile.mkdtemp()) / "ret004-diag-coord.db"
    )

    run_id = new_run_id()
    beir_ns = f"evalrun-{run_id}/ret004diag/episodic"
    sched_ns: str | None = None
    out: dict[str, Any] = {"run_id": run_id, "limit": DIAG_LIMIT, "beir": [], "scheduled": []}

    try:
        # ---------- BEIR 16 groups: mature-only retrieval (default state filter) ----------
        groups = _beir_query_groups()
        seeded: set[str] = set()
        beir_rows: list[dict[str, Any]] = []
        for g in groups:
            ans = asyncio.run(
                plane.create(EpisodicMemory(namespace=beir_ns, content=g.target, state="matured"))
            )
            target_id = str(ans.object_id)
            seeded.add(target_id)
            distractor_ids: list[str] = []
            for d in g.distractors:
                w = asyncio.run(
                    plane.create(EpisodicMemory(namespace=beir_ns, content=d, state="matured"))
                )
                seeded.add(str(w.object_id))
                distractor_ids.append(str(w.object_id))
            beir_rows.append(
                {
                    "query": g.query,
                    "hybrid_favorable": g.hybrid_favorable,
                    "target": target_id,
                    "distractors": distractor_ids,
                }
            )
        # canonical maturation (create() forces provisional; default hybrid_search excludes it)
        for oid in seeded:
            oc = asyncio.run(
                plane.transition(
                    namespace=beir_ns,
                    object_id=oid,
                    to_state="matured",
                    actor="ret004-diag",
                    reason="diagnostic maturation",
                    coordinator=coordinator,
                )
            )
            if isinstance(oc, Err):
                raise AssertionError(f"BEIR maturation failed for {oid}: {oc}")
        asyncio.run(wait_for_visibility(client, collection, beir_ns, expected_count=len(seeded)))

        for row in beir_rows:
            caps = _capture_three(
                client,
                embedder,
                namespace=beir_ns,
                query=row["query"],
                collection=collection,
                state_filter=None,  # mature-only default
            )
            row.update(caps)
            out["beir"].append(row)

        # ---------- Scheduled corpus: provisional+matured visibility (as the scheduled gate does) ----------
        data_dir = Path(__file__).resolve().parents[1] / "evals" / "data"
        corpus = load_corpus(data_dir)
        sched_run = new_run_id()
        sched_ns = run_namespace("episodic", run_id=sched_run)

        def _plane_factory(_plane_name: str) -> EpisodicPlane:
            return plane

        # _seed_documents seeds into run_namespace("episodic", run_id=sched_run)
        key_to_id = asyncio.run(
            _seed_documents(corpus, plane_factory=_plane_factory, run_id=sched_run)
        )
        asyncio.run(
            wait_for_visibility(client, collection, sched_ns, expected_count=len(key_to_id))
        )
        for query in corpus.queries:
            caps = _capture_three(
                client,
                embedder,
                namespace=sched_ns,
                query=query.text,
                collection=collection,
                state_filter=("provisional", "matured"),  # matches the scheduled gate visibility
            )
            out["scheduled"].append(
                {
                    "id": query.id,
                    "mode": query.mode,
                    "behavior": query.behavior,
                    "relevant": [
                        {"id": key_to_id.get(ref.key, ""), "relevance": ref.relevance}
                        for ref in query.relevant
                    ],
                    **caps,
                }
            )

        print("RET004-DIAG-JSON-START")
        print(json.dumps(out))
        print("RET004-DIAG-JSON-END")
    finally:
        _teardown_namespace(client, collection, beir_ns)
        if sched_ns is not None:
            _teardown_namespace(client, collection, sched_ns)
