"""RET-004 static corpus-discrimination contract (Yua ruling 2026-07-15).

The ONE canonical scheduled corpus must be structurally HARD, not just infrastructure proof. These
tests run NO TEI — they check structure and defeat a naive lexical baseline, so a too-easy corpus
(all-near-verbatim, too few queries, no hard negatives, or one a dumb exact-token ranker clears) is
rejected before it can pass a real gate. Metrics being 1.0 on the real run is fine; a corpus a naive
ranker also clears is not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from musubi.evals.live_gate import aggregate, enforce_thresholds, evaluate_query
from musubi.evals.scheduled_gate import CorpusQuery, ScheduledCorpus, load_corpus

_DATA_DIR = Path(__file__).parent / "data"


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _overlap(query: str, content: str) -> int:
    return len(_tokens(query) & _tokens(content))


def _target_key(query: CorpusQuery) -> str:
    return next(ref.key for ref in query.relevant if ref.relevance == 3)


def _naive_lexical_metrics(corpus: ScheduledCorpus) -> dict[str, dict[str, float]]:
    """Score a deliberately naive ranker: rank documents by raw query/content token overlap (keys
    stand in for object_ids). This is the dumb baseline the corpus must defeat."""
    docs = {doc.key: doc.content for doc in corpus.documents}
    by_mode: dict[str, list[dict[str, float]]] = {}
    for query in corpus.queries:
        ranked = [
            key
            for key, _ in sorted(docs.items(), key=lambda kv: (-_overlap(query.text, kv[1]), kv[0]))
        ]
        relevant = [{"object_id": ref.key, "relevance": ref.relevance} for ref in query.relevant]
        by_mode.setdefault(query.mode, []).append(evaluate_query(ranked, relevant))
    return {mode: aggregate(rows) for mode, rows in by_mode.items()}


def _load() -> ScheduledCorpus:
    return load_corpus(_DATA_DIR)


# --- structural requirements ----------------------------------------------------------------------


def test_corpus_has_enough_graded_queries_across_modes() -> None:
    corpus = _load()
    assert len(corpus.queries) >= 8, "need >= 8 graded queries"
    assert {"fast", "deep"} <= {q.mode for q in corpus.queries}, "must cover fast AND deep"
    for query in corpus.queries:
        targets = [ref for ref in query.relevant if ref.relevance == 3]
        assert len(targets) == 1, f"query {query.id!r} needs exactly one relevance-3 target"


def test_at_least_half_queries_are_not_near_verbatim() -> None:
    """>= half the queries must NOT be near-verbatim restatements of their target — a query whose
    tokens are nearly a subset of the target's is trivial."""
    corpus = _load()
    docs = {doc.key: doc.content for doc in corpus.documents}
    non_verbatim = 0
    for query in corpus.queries:
        q_tokens = _tokens(query.text)
        target_tokens = _tokens(docs[_target_key(query)])
        coverage = len(q_tokens & target_tokens) / max(1, len(q_tokens))
        if coverage < 0.6:  # the query is not just the target restated
            non_verbatim += 1
    assert non_verbatim >= len(corpus.queries) / 2, (
        f"only {non_verbatim}/{len(corpus.queries)} queries are non-verbatim; need >= half"
    )


def test_a_surface_overlap_distractor_outranks_a_target_somewhere() -> None:
    """At least one query must have an UNLABELED doc with STRONGER surface-token overlap than its
    relevance-3 target — the hard-negative that trips a lexical ranker."""
    corpus = _load()
    docs = {doc.key: doc.content for doc in corpus.documents}
    found = False
    for query in corpus.queries:
        labeled = {ref.key for ref in query.relevant}
        target_overlap = _overlap(query.text, docs[_target_key(query)])
        for key, content in docs.items():
            if key not in labeled and _overlap(query.text, content) > target_overlap:
                found = True
                break
    assert found, "no surface-overlap hard-negative outranks a target — corpus too easy for lexical"


# --- THE wrong-ranker discriminator ---------------------------------------------------------------


def test_naive_lexical_ranker_does_not_clear_the_frozen_thresholds() -> None:
    """The corpus must DEFEAT a naive exact-token-overlap ranker — if a dumb baseline clears the
    frozen thresholds, the corpus proves nothing about real retrieval quality."""
    naive = _naive_lexical_metrics(_load())
    with pytest.raises(ValueError):  # naive ranker falls below the frozen thresholds somewhere
        enforce_thresholds(naive)


# --- discriminators: prove the contract REJECTS degenerate corpora --------------------------------

_VERBATIM_CORPUS = ScheduledCorpus.model_validate(
    {
        "documents": [
            {
                "key": f"d{i}",
                "plane": "episodic",
                "state": "matured",
                "content": f"exact answer number {i}",
            }
            for i in range(3)
        ],
        "queries": [
            {
                "id": f"q{i}",
                "text": f"exact answer number {i}",  # query IS its target verbatim
                "mode": "fast" if i % 2 else "deep",
                "relevant": [{"key": f"d{i}", "relevance": 3}],
            }
            for i in range(3)
        ],
    }
)


def test_contract_rejects_a_verbatim_corpus_via_structural_and_naive_checks() -> None:
    """A corpus where every query is verbatim its target (and there are too few queries) must be
    rejected: too few queries, all near-verbatim, and a naive ranker clears it."""
    corpus = _VERBATIM_CORPUS
    # too few queries
    assert not len(corpus.queries) >= 8
    # all near-verbatim (query tokens fully cover the target)
    docs = {d.key: d.content for d in corpus.documents}
    verbatim = sum(
        1
        for q in corpus.queries
        if len(_tokens(q.text) & _tokens(docs[_target_key(q)])) / max(1, len(_tokens(q.text)))
        >= 0.6
    )
    assert verbatim == len(corpus.queries)  # every query is near-verbatim
    # a naive ranker CLEARS this easy corpus (so it would NOT be caught by quality alone)
    enforce_thresholds(_naive_lexical_metrics(corpus))  # does not raise → proves it's too easy
