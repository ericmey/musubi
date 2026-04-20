"""Deterministic 10k-memory synthetic corpus generator.

Per the slice file's Implementation notes — a fixed-seed template
expansion produces the same 10,000 memories every run so perf
bullets (#13, #14) measure against a stable baseline. Identifiers
are KSUIDs derived from the row index so re-running on a fresh
Qdrant produces the same point IDs.

The generator is intentionally CPU-trivial — it builds payloads;
embedding + upsert happen in the harness fixture so the cost is
bounded by Qdrant + TEI throughput, not by template expansion.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass

_NAMESPACES: tuple[str, ...] = (
    "eric/claude-code/episodic",
    "eric/livekit-voice/episodic",
    "eric/openclaw/episodic",
    "eric/_shared/episodic",
)

_TOPICS: tuple[str, ...] = (
    "infrastructure/gpu",
    "infrastructure/networking",
    "infrastructure/storage",
    "engineering/python",
    "engineering/typescript",
    "engineering/rust",
    "engineering/go",
    "lifecycle/maturation",
    "lifecycle/synthesis",
    "lifecycle/promotion",
    "retrieval/fast",
    "retrieval/deep",
    "vault/sync",
    "vault/echo-filter",
    "voice/transcripts",
    "voice/speech-generation",
    "ops/observability",
    "ops/backup",
    "ops/compose",
    "security/auth",
)

_TEMPLATES: tuple[str, ...] = (
    "Recorded an experiment on {topic} — outcome was {outcome}.",
    "Question raised about {topic}: how does it interact with {related_topic}?",
    "Decision: prefer {choice_a} over {choice_b} for {topic} per the Apr-2026 review.",
    "Observation: {topic} latency drifted up after the Tuesday deploy.",
    "Snippet from a coding session — wrote a helper for {topic} that handles {edge_case}.",
    "Cross-reference: the {topic} pattern shows up again in {related_topic}.",
    "Reminder: re-check the {topic} threshold after the next {trigger} event.",
    "Insight: most {topic} regressions trace back to a {root_cause} in {related_topic}.",
)

_FILLERS: dict[str, tuple[str, ...]] = {
    "outcome": (
        "clean",
        "flaky",
        "ten-percent regression",
        "two-x speedup",
        "no measurable change",
    ),
    "choice_a": (
        "immutable dataclasses",
        "explicit timeouts",
        "structured logging",
        "small models",
    ),
    "choice_b": (
        "ad-hoc dicts",
        "default httpx timeouts",
        "f-string logging",
        "kitchen-sink models",
    ),
    "edge_case": ("empty input", "unicode normalisation", "large payloads", "concurrent writers"),
    "trigger": ("ollama upgrade", "qdrant rebuild", "TEI model swap", "kong ACL change"),
    "root_cause": ("bad cache key", "missing index", "off-by-one window", "lock ordering"),
}


@dataclass(frozen=True)
class CorpusEntry:
    object_id: str
    namespace: str
    content: str
    topics: tuple[str, ...]
    importance: int


def _derive_object_id(idx: int) -> str:
    """27-char-ish KSUID-shaped id derived from the row index — stable
    across re-runs so test assertions against specific points stay
    valid."""
    digest = hashlib.sha256(f"musubi-test-corpus:{idx}".encode()).hexdigest()
    return digest[:27]


def _pick(seq: tuple[str, ...], idx: int, salt: int) -> str:
    return seq[(idx * 31 + salt * 17) % len(seq)]


def _render_content(idx: int) -> tuple[str, tuple[str, str]]:
    template = _TEMPLATES[idx % len(_TEMPLATES)]
    topic = _pick(_TOPICS, idx, salt=1)
    related_topic = _pick(_TOPICS, idx, salt=7)
    fillers = {
        "topic": topic,
        "related_topic": related_topic,
        **{k: _pick(v, idx, salt=hash(k) & 0xFFFF) for k, v in _FILLERS.items()},
    }
    return template.format(**fillers), (topic, related_topic)


def generate_corpus(count: int = 10_000) -> Iterator[CorpusEntry]:
    """Yield ``count`` deterministic CorpusEntry rows. Re-running with
    the same ``count`` yields exactly the same sequence."""
    for idx in range(count):
        content, (topic_a, topic_b) = _render_content(idx)
        namespace = _NAMESPACES[idx % len(_NAMESPACES)]
        importance = (idx % 8) + 2  # range 2-9, never 1 or 10
        yield CorpusEntry(
            object_id=_derive_object_id(idx),
            namespace=namespace,
            content=content,
            topics=(topic_a, topic_b),
            importance=importance,
        )


__all__ = ["CorpusEntry", "generate_corpus"]
