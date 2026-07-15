"""RET-004 self-seeding scheduled quality gate.

One canonical checksum-pinned graded corpus + one self-seeding runner (Yua ruling 2026-07-15). The
scheduled gate does NOT query a pre-seeded store: it validates the corpus schema + checksum, mints a
fresh run-scoped valid 3-segment namespace, seeds the labelled documents into real Qdrant through the
PRODUCTION write seam (:meth:`EpisodicPlane.create`), waits for visibility, runs per-mode retrieval
metrics against the frozen thresholds, then tears down ONLY that run-owned namespace's data.

Everything except the real quality NUMBERS is deterministic and verifiable locally with a real Qdrant
and a FakeEmbedder (the seed→visibility→measure→teardown mechanism); the real numbers run on the
scheduled x86 TEI CI. Thresholds are frozen — a failing run returns raw per-mode results + corpus
attribution, never a tuned-to-green pass.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from qdrant_client import models

from musubi.evals.corpus import verify_manifest
from musubi.evals.live_gate import aggregate, evaluate_query
from musubi.types.common import LifecycleState

_VISIBILITY_ATTEMPTS = 30  # bounded polls for seeded rows to become queryable before fail-loud
_VISIBILITY_BACKOFF_S = 0.5


class ScheduledGateFailure(RuntimeError):
    """A self-seeding scheduled run could not complete (bad corpus, seed failure, visibility timeout,
    or a scoped-teardown violation). Raised so the gate FAILS LOUD rather than reporting a partial or
    invented result."""


class GradedRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1)  # references a document `key`, resolved to its real object_id
    relevance: StrictInt = Field(ge=0, le=3)


class CorpusDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1)  # stable handle a query's relevant[] points at
    plane: str = Field(min_length=1)
    state: str = Field(min_length=1)
    content: str = Field(min_length=1)


class CorpusQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    mode: str = Field(min_length=1)
    relevant: list[GradedRef] = Field(min_length=1)


class ScheduledCorpus(BaseModel):
    """A representative, multi-query graded corpus. Documents are seeded; queries are scored."""

    model_config = ConfigDict(extra="forbid")
    documents: list[CorpusDocument] = Field(min_length=2)
    queries: list[CorpusQuery] = Field(min_length=2)

    def validate_references(self) -> None:
        """Every query's relevant keys must resolve to a declared document (fail-closed schema)."""
        known = {document.key for document in self.documents}
        for query in self.queries:
            for ref in query.relevant:
                if ref.key not in known:
                    raise ScheduledGateFailure(
                        f"query {query.id!r} references unknown document key {ref.key!r}"
                    )


def load_corpus(data_dir: Path, *, corpus_name: str = "scheduled_corpus.yaml") -> ScheduledCorpus:
    """Validate the manifest checksum for ``corpus_name`` (checksum-drift fails loud), then load and
    schema-validate the graded corpus."""
    manifest_path = data_dir / "manifest.json"
    corpus_path = data_dir / corpus_name
    if not manifest_path.exists() or not corpus_path.exists():
        raise ScheduledGateFailure(f"missing {corpus_name} or manifest.json in {data_dir}")
    import json

    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ScheduledGateFailure(f"manifest load failed: {exc}") from exc
    if corpus_name not in manifest.get("files", {}):
        raise ScheduledGateFailure(f"{corpus_name} is not checksum-pinned in the manifest")
    try:
        verify_manifest(manifest, data_dir)  # raises ValueError on checksum drift
    except ValueError as exc:
        raise ScheduledGateFailure(f"corpus checksum verification failed: {exc}") from exc
    try:
        corpus = ScheduledCorpus.model_validate(yaml.safe_load(corpus_path.read_text()))
    except Exception as exc:
        raise ScheduledGateFailure(f"corpus schema validation failed: {exc}") from exc
    corpus.validate_references()
    return corpus


def run_namespace(plane: str, *, run_id: str) -> str:
    """A fresh, valid 3-segment run-scoped namespace: ``evalrun-<run_id>/scheduled/<plane>``. Distinct
    per run so seeding never collides with real data, and teardown can be scoped to exactly it."""
    return f"evalrun-{run_id}/scheduled/{plane}"


def new_run_id() -> str:
    return secrets.token_hex(6)


async def _seed_documents(
    corpus: ScheduledCorpus, *, plane_factory: Any, run_id: str
) -> dict[str, str]:
    """Seed each document through its plane's PRODUCTION create seam; return ``{key: object_id}``.

    ``plane_factory(plane_name)`` yields a plane whose ``.create(...)`` is the real write path. Only
    the episodic plane is seeded in this pass (matured + provisional states); an unsupported plane
    fails loud rather than silently skipping graded content."""
    from musubi.types.episodic import EpisodicMemory

    key_to_object_id: dict[str, str] = {}
    for document in corpus.documents:
        if document.plane != "episodic":
            raise ScheduledGateFailure(
                f"document {document.key!r} targets unsupported plane {document.plane!r}"
            )
        plane = plane_factory("episodic")
        try:
            written = await plane.create(
                EpisodicMemory(
                    namespace=run_namespace("episodic", run_id=run_id),
                    content=document.content,
                    # Runtime-validated by EpisodicMemory (invalid state → ValidationError → caught);
                    # cast because the episodic state set is narrower than LifecycleState.
                    state=cast("Any", document.state),
                )
            )
        except Exception as exc:
            raise ScheduledGateFailure(
                f"seed failed for document {document.key!r}: {exc!r}"
            ) from exc
        key_to_object_id[document.key] = str(written.object_id)
    return key_to_object_id


async def wait_for_visibility(
    client: Any, collection: str, namespace: str, *, expected_count: int
) -> None:
    """Poll until at least ``expected_count`` distinct object_ids are queryable in ``namespace``,
    bounded, else fail loud. The SHARED visibility semantics for the scheduled gate AND the BEIR
    integration path — freshly-seeded rows aren't instantly queryable, and measuring a half-seeded
    store yields 0/0. Callers pass the ACTUAL distinct seeded count (dedup can reduce it below the
    document count), never a raw document total."""

    async def _count() -> int:
        records, _ = client.scroll(
            collection_name=collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace))
                ]
            ),
            limit=10_000,
            with_payload=["object_id"],
            with_vectors=False,
        )
        return len({(rec.payload or {}).get("object_id") for rec in records})

    for attempt in range(_VISIBILITY_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_VISIBILITY_BACKOFF_S)
        if await _count() >= expected_count:
            return
    raise ScheduledGateFailure(
        f"seeded rows never became visible ({expected_count} expected) in {namespace!r} after "
        f"{_VISIBILITY_ATTEMPTS} polls — refusing to measure a half-seeded store"
    )


async def _measure(
    corpus: ScheduledCorpus, key_to_object_id: dict[str, str], *, retrieve: Any
) -> tuple[dict[str, dict[str, float]], list[dict[str, Any]]]:
    """Retrieve + score each query. Returns ``(per_mode_aggregate, per_query)`` — Yua requires the
    per-query results, not only aggregates, so a failing run can be attributed to specific queries."""
    by_mode: dict[str, list[dict[str, float]]] = {}
    per_query: list[dict[str, Any]] = []
    for query in corpus.queries:
        ordered_ids = await retrieve(query.text, query.mode)
        relevant = [
            {"object_id": key_to_object_id[ref.key], "relevance": ref.relevance}
            for ref in query.relevant
        ]
        metrics = evaluate_query(ordered_ids, relevant)
        by_mode.setdefault(query.mode, []).append(metrics)
        per_query.append({"id": query.id, "mode": query.mode, "metrics": metrics})
    return {mode: aggregate(rows) for mode, rows in by_mode.items()}, per_query


def _teardown(client: Any, collection: str, namespace: str) -> None:
    """Delete ONLY the run-owned data: points whose namespace equals the exact run namespace. Scoped
    by filter so a bug can never reach another namespace's rows (teardown owner-scope)."""
    if "evalrun-" not in namespace:
        # Guard: teardown must only ever target a run-scoped namespace, never real data.
        raise ScheduledGateFailure(
            f"refusing to tear down non-run namespace {namespace!r} — owner-scope violation"
        )
    client.delete(
        collection_name=collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace))
                ]
            )
        ),
    )


async def run_scheduled_seeded_gate(
    backends: Any, *, data_dir: Path, run_id: str
) -> dict[str, Any]:
    """The full self-seeding scheduled measurement: validate+checksum the corpus, seed it into a
    fresh run-scoped namespace via the production write seam, wait for visibility, measure metrics,
    and tear down ONLY the run-owned data (even on failure). Returns
    ``{"by_mode": <per-mode aggregate>, "per_query": [...]}`` — the caller enforces the frozen
    thresholds on ``by_mode`` and never tunes them; per-query is for attribution."""
    from musubi.evals.live_gate import _hits_or_raise
    from musubi.retrieve.deep import RetrievalQuery, run_deep_retrieve
    from musubi.retrieve.fast import run_fast_retrieve
    from musubi.store import bootstrap as bootstrap_collections
    from musubi.store.names import collection_for_plane

    corpus = load_corpus(data_dir)
    # Ensure the canonical collections exist before seeding — a fresh CI Qdrant has none, and an
    # upsert into a missing collection fails (the production api.bootstrap does the same). Idempotent.
    bootstrap_collections(backends.client)
    namespace = run_namespace("episodic", run_id=run_id)
    collection = collection_for_plane("episodic")
    plane_factory = _episodic_plane_factory(backends)

    async def _retrieve(query_text: str, mode: str) -> list[str]:
        # Provisional included so the immediate-recall contract is exercised.
        state_filter: tuple[LifecycleState, ...] = ("provisional", "matured")
        if mode == "deep":
            result: Any = await run_deep_retrieve(
                backends.client,
                backends.embedder,
                backends.reranker,
                RetrievalQuery(
                    namespace=namespace,
                    query_text=query_text,
                    mode="deep",
                    limit=20,
                    state_filter=state_filter,
                ),
            )
        else:
            result = await run_fast_retrieve(
                backends.client,
                backends.embedder,
                namespace=namespace,
                query=query_text,
                collections=(collection,),
                limit=20,
                state_filter=state_filter,
                # This gate measures ranking QUALITY, not latency — the interactive 250ms per-plane
                # default 503s on the cold CPU-TEI CI stack. Give retrieval generous headroom so a
                # slow embed can't fail the quality measurement (latency has its own contracts).
                plane_timeout_s=30.0,
                sparse_timeout_s=30.0,
            )
        return _hits_or_raise(result, query_text)

    try:
        key_to_object_id = await _seed_documents(corpus, plane_factory=plane_factory, run_id=run_id)
        await wait_for_visibility(
            backends.client,
            collection,
            namespace,
            expected_count=len(set(key_to_object_id.values())),
        )
        by_mode, per_query = await _measure(corpus, key_to_object_id, retrieve=_retrieve)
        return {"by_mode": by_mode, "per_query": per_query}
    finally:
        # Best-effort teardown: a cleanup failure must NEVER mask the original gate error (or a real
        # measurement). Surface it as a log line and let the original propagate.
        try:
            _teardown(backends.client, collection, namespace)
        except Exception as teardown_exc:
            logging.getLogger(__name__).warning(
                "scheduled-gate teardown failed for %s: %r — original result/error preserved",
                namespace,
                teardown_exc,
            )


def _episodic_plane_factory(backends: Any) -> Any:
    from musubi.planes.episodic.plane import EpisodicPlane

    def factory(plane: str) -> Any:
        if plane != "episodic":
            raise ScheduledGateFailure(f"no production seam wired for plane {plane!r}")
        return EpisodicPlane(client=backends.client, embedder=backends.embedder)

    return factory


__all__ = [
    "CorpusDocument",
    "CorpusQuery",
    "GradedRef",
    "ScheduledCorpus",
    "ScheduledGateFailure",
    "aggregate",
    "evaluate_query",
    "load_corpus",
    "new_run_id",
    "run_namespace",
    "run_scheduled_seeded_gate",
    "wait_for_visibility",
]
