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


async def _wait_visible(expected_object_ids: set[str], *, count_visible: Any) -> None:
    """Poll ``count_visible()`` until every seeded object_id is queryable, bounded. A row that never
    becomes visible fails loud rather than measuring against a half-seeded store."""
    for attempt in range(_VISIBILITY_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_VISIBILITY_BACKOFF_S)
        if await count_visible() >= len(expected_object_ids):
            return
    raise ScheduledGateFailure(
        f"seeded rows never became visible ({len(expected_object_ids)} expected) after "
        f"{_VISIBILITY_ATTEMPTS} polls — refusing to measure a half-seeded store"
    )


async def _measure(
    corpus: ScheduledCorpus, key_to_object_id: dict[str, str], *, retrieve: Any
) -> dict[str, dict[str, float]]:
    """Per-mode metrics: retrieve each query (scoped to the run namespace, provisional included) and
    score its ranked object_ids against the graded refs. ``retrieve(query_text, mode)`` returns the
    ranked object_id list from the live pipeline."""
    by_mode: dict[str, list[dict[str, float]]] = {}
    for query in corpus.queries:
        ordered_ids = await retrieve(query.text, query.mode)
        relevant = [
            {"object_id": key_to_object_id[ref.key], "relevance": ref.relevance}
            for ref in query.relevant
        ]
        by_mode.setdefault(query.mode, []).append(evaluate_query(ordered_ids, relevant))
    return {mode: aggregate(rows) for mode, rows in by_mode.items()}


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
) -> dict[str, dict[str, float]]:
    """The full self-seeding scheduled measurement: validate+checksum the corpus, seed it into a
    fresh run-scoped namespace via the production write seam, wait for visibility, measure per-mode
    metrics, and tear down ONLY the run-owned data (even on failure). Returns per-mode metrics; the
    caller enforces the frozen thresholds and never tunes them."""
    from musubi.evals.live_gate import _hits_or_raise
    from musubi.retrieve.deep import RetrievalQuery, run_deep_retrieve
    from musubi.retrieve.fast import run_fast_retrieve
    from musubi.store.names import collection_for_plane

    corpus = load_corpus(data_dir)
    namespace = run_namespace("episodic", run_id=run_id)
    collection = collection_for_plane("episodic")
    plane_factory = _episodic_plane_factory(backends)

    def _count_visible() -> Any:
        async def _count() -> int:
            records, _ = backends.client.scroll(
                collection_name=collection,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="namespace", match=models.MatchValue(value=namespace)
                        )
                    ]
                ),
                limit=10_000,
                with_payload=["object_id"],
                with_vectors=False,
            )
            return len({(rec.payload or {}).get("object_id") for rec in records})

        return _count()

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
            )
        return _hits_or_raise(result, query_text)

    try:
        key_to_object_id = await _seed_documents(corpus, plane_factory=plane_factory, run_id=run_id)
        await _wait_visible(set(key_to_object_id.values()), count_visible=_count_visible)
        return await _measure(corpus, key_to_object_id, retrieve=_retrieve)
    finally:
        _teardown(backends.client, collection, namespace)


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
]
