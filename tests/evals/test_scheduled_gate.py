"""RET-004 self-seeding scheduled gate — the six required discriminators (unit) + a full-mechanism
integration test (real Qdrant + FakeEmbedder) proving seed→visibility→measure→teardown end to end.

The discriminators cover: checksum drift, teardown owner-scope, seed failure, visibility timeout,
invalid namespace, and no-seed. The real quality NUMBERS come from the scheduled x86 TEI run; here we
prove the MECHANISM is honest — it never measures a half-seeded store, never tears down real data,
and never passes without a real seed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from musubi.evals import scheduled_gate as sg
from musubi.evals.scheduled_gate import (
    ScheduledCorpus,
    ScheduledGateFailure,
    _measure,
    _seed_documents,
    _teardown,
    _wait_visible,
    load_corpus,
    run_namespace,
)
from musubi.types.common import validate_namespace

_CORPUS_YAML = """
documents:
  - {key: d1, plane: episodic, state: matured, content: "first document about alpha"}
  - {key: d2, plane: episodic, state: provisional, content: "second document about beta"}
queries:
  - id: q1
    text: "alpha"
    mode: fast
    relevant:
      - {key: d1, relevance: 3}
  - id: q2
    text: "beta"
    mode: deep
    relevant:
      - {key: d2, relevance: 3}
"""


def _write_corpus(data_dir: Path, *, drift: bool = False) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    corpus = data_dir / "scheduled_corpus.yaml"
    corpus.write_text(_CORPUS_YAML, encoding="utf-8")
    checksum = hashlib.sha256(corpus.read_bytes()).hexdigest()
    if drift:
        checksum = "0" * 64  # manifest pins a stale hash — checksum drift
    (data_dir / "manifest.json").write_text(
        json.dumps({"name": "t", "files": {"scheduled_corpus.yaml": checksum}}), encoding="utf-8"
    )


# --- discriminator 1: checksum drift ---------------------------------------------------------------


def test_checksum_drift_fails_loud(tmp_path: Path) -> None:
    _write_corpus(tmp_path, drift=True)
    with pytest.raises(ScheduledGateFailure, match="checksum"):
        load_corpus(tmp_path)


def test_valid_corpus_loads(tmp_path: Path) -> None:
    _write_corpus(tmp_path)
    corpus = load_corpus(tmp_path)
    assert {d.key for d in corpus.documents} == {"d1", "d2"}


# --- discriminator 2: teardown owner-scope ---------------------------------------------------------


class _RecordingClient:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, *, collection_name: str, points_selector: Any) -> None:
        self.deleted.append(collection_name)


def test_teardown_refuses_non_run_namespace() -> None:
    """Owner-scope: teardown must NEVER target a namespace that isn't run-owned (no evalrun- prefix),
    or a bug could delete real data."""
    client = _RecordingClient()
    with pytest.raises(ScheduledGateFailure, match="owner-scope"):
        _teardown(client, "musubi_episodic", "aoi/command-chair/episodic")
    assert client.deleted == []  # the real-namespace delete never fired


def test_teardown_deletes_only_the_run_namespace() -> None:
    client = _RecordingClient()
    _teardown(client, "musubi_episodic", run_namespace("episodic", run_id="abc123"))
    assert client.deleted == ["musubi_episodic"]  # scoped delete fired for the run namespace


# --- discriminator 3: seed failure -----------------------------------------------------------------


def test_seed_failure_fails_loud() -> None:
    corpus = ScheduledCorpus.model_validate(yaml.safe_load(_CORPUS_YAML))

    class _BoomPlane:
        async def create(self, _memory: Any) -> Any:
            raise RuntimeError("qdrant write refused")

    with pytest.raises(ScheduledGateFailure, match="seed failed"):
        asyncio.run(_seed_documents(corpus, plane_factory=lambda _p: _BoomPlane(), run_id="abc123"))


# --- discriminator 4: visibility timeout -----------------------------------------------------------


def test_visibility_timeout_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sg, "_VISIBILITY_ATTEMPTS", 3)
    monkeypatch.setattr(sg, "_VISIBILITY_BACKOFF_S", 0.0)

    async def _never_visible() -> int:
        return 0  # seeded rows never appear

    with pytest.raises(ScheduledGateFailure, match="never became visible"):
        asyncio.run(_wait_visible({"a", "b"}, count_visible=_never_visible))


# --- discriminator 5: invalid namespace ------------------------------------------------------------


def test_run_namespace_is_valid_three_segment() -> None:
    """The run namespace must be a valid 3-segment tenant/presence/plane — the original stub used an
    invalid 2-segment 'test/ns', which retrieval rejected."""
    ns = run_namespace("episodic", run_id="abc123")
    assert ns.count("/") == 2
    assert validate_namespace(ns) == ns  # accepted by the production validator


def test_invalid_two_segment_namespace_is_rejected() -> None:
    with pytest.raises(ValueError, match="tenant/presence/plane"):
        validate_namespace("test/ns")  # the exact stub that broke the first live run


# --- discriminator 6: no-seed (an unseeded/empty measurement must FAIL, never fake-pass) -----------


def test_measure_of_empty_store_yields_failing_metrics() -> None:
    """If retrieval returns nothing (nothing seeded/visible), per-mode metrics are all zero — which
    the frozen thresholds reject. The gate can never pass without a real seed."""
    from musubi.evals.live_gate import enforce_thresholds

    corpus = ScheduledCorpus.model_validate(yaml.safe_load(_CORPUS_YAML))

    async def _empty_retrieve(_text: str, _mode: str) -> list[str]:
        return []

    by_mode = asyncio.run(
        _measure(corpus, {"d1": "oid-d1", "d2": "oid-d2"}, retrieve=_empty_retrieve)
    )
    assert by_mode["fast"]["ndcg@10"] == 0.0  # nothing retrieved → zero
    with pytest.raises(ValueError, match="below threshold"):
        enforce_thresholds(by_mode)


# --- full mechanism: real Qdrant + FakeEmbedder (seed→visibility→measure→teardown) -----------------


@pytest.mark.integration
def test_scheduled_seeded_gate_full_mechanism_local() -> None:
    """End-to-end mechanism against a REAL Qdrant with a FakeEmbedder: the canonical corpus seeds,
    becomes visible, is measured per-mode, and is fully torn down. Proves the machinery honestly;
    the real quality NUMBERS (which need TEI) run on the scheduled x86 CI."""
    import os

    from qdrant_client import QdrantClient, models

    from musubi.embedding import FakeEmbedder
    from musubi.evals.live_gate import LiveBackends
    from musubi.store.names import collection_for_plane

    port = int(os.environ.get("MUSUBI_TEST_QDRANT_PORT", "6339"))
    client = QdrantClient(host="localhost", port=port)
    # NOTE: no manual bootstrap — the gate must bootstrap its own collections (a fresh CI Qdrant has
    # none). Relying on the gate here keeps this test faithful to the real scheduled path.
    embedder = FakeEmbedder()
    backends = LiveBackends(client=client, embedder=embedder, reranker=embedder)
    run_id = sg.new_run_id()
    namespace = run_namespace("episodic", run_id=run_id)
    data_dir = Path(__file__).parent / "data"

    try:
        by_mode = asyncio.run(
            sg.run_scheduled_seeded_gate(backends, data_dir=data_dir, run_id=run_id)
        )
        # Both modes in the canonical corpus were measured with the full metric set.
        assert set(by_mode) == {"fast", "deep"}
        for metrics in by_mode.values():
            assert {"ndcg@10", "mrr", "recall@20", "p@1"} <= set(metrics)
    finally:
        # Teardown ran in the gate's finally; the run namespace must be empty.
        records, _ = client.scroll(
            collection_name=collection_for_plane("episodic"),
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace))
                ]
            ),
            limit=100,
        )
        assert records == [], "teardown must leave the run namespace empty"
        client.close()
