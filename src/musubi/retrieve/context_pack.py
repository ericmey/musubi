"""Ranked context packs for Musubi essence alignment.

This module is intentionally independent of Qdrant / FastAPI. The API
router adapts retrieval payloads into :class:`ContextCandidate`, then this
module owns the v1 ranking contract: closed kinds, staleness suppression,
BM25 lexical relevance, durable tiebreaks, grouped output, and char caps.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

EssenceKind = Literal[
    "boundary",
    "operating-rule",
    "identity-principle",
    "relationship/care-cue",
    "project-stance",
    "open-loop",
    "tool/runtime-fact",
    "correction/suppression",
    "episode",
]
StalenessTier = Literal["durable", "current", "episodic", "superseded"]
ContextMode = Literal["startup"]

VALID_KINDS: frozenset[str] = frozenset(
    {
        "boundary",
        "operating-rule",
        "identity-principle",
        "relationship/care-cue",
        "project-stance",
        "open-loop",
        "tool/runtime-fact",
        "correction/suppression",
        "episode",
    }
)

VALID_STALENESS: frozenset[str] = frozenset({"durable", "current", "episodic", "superseded"})

_KIND_PRIORITY: dict[EssenceKind, float] = {
    "boundary": 10.0,
    "operating-rule": 9.0,
    "identity-principle": 8.0,
    "relationship/care-cue": 7.0,
    "open-loop": 6.0,
    "project-stance": 5.0,
    "tool/runtime-fact": 4.5,
    "correction/suppression": 4.0,
    "episode": 1.0,
}

_STALENESS_PRIORITY: dict[StalenessTier, float] = {
    "durable": 4.0,
    "current": 3.0,
    "episodic": 1.0,
    "superseded": -8.0,
}

_GROUP_BY_KIND: dict[EssenceKind, str] = {
    "boundary": "Must-Obey",
    "operating-rule": "Must-Obey",
    "identity-principle": "Relationship-Voice",
    "relationship/care-cue": "Relationship-Voice",
    "project-stance": "Current-Project",
    "open-loop": "Open-Loops",
    "tool/runtime-fact": "Tool-Runtime",
    "correction/suppression": "Recent-Corrections",
    "episode": "Context",
}

_GROUP_ORDER: tuple[str, ...] = (
    "Must-Obey",
    "Current-Project",
    "Relationship-Voice",
    "Open-Loops",
    "Tool-Runtime",
    "Recent-Corrections",
    "Context",
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.+-]*", re.IGNORECASE)


class ContextPackQuery(BaseModel):
    """Request shape for building a small context pack."""

    query_text: str = ""
    mode: ContextMode = "startup"
    max_items: int = Field(default=8, ge=1, le=50)
    max_chars: int = Field(default=1200, ge=120, le=8000)
    include_history: bool = False
    recent_reserve: int = Field(default=0, ge=0, le=50)


class ContextCandidate(BaseModel):
    """A retrieval row normalised for essence ranking."""

    object_id: str
    lane: Literal["recent", "ranked"] = "ranked"
    namespace: str
    plane: str
    content: str
    summary: str | None = None
    title: str | None = None
    tags: list[str] = Field(default_factory=list)
    state: str = "matured"
    created_epoch: float = 0.0
    updated_epoch: float | None = None
    importance: int = 5
    retrieve_score: float = 0.0
    extra: dict[str, Any] = Field(default_factory=dict)


class ContextPackItem(BaseModel):
    """One surfaced context item."""

    object_id: str
    namespace: str
    plane: str
    kind: EssenceKind
    staleness: StalenessTier
    content: str
    evidence_handle: str
    why_surfaced: str
    score: float
    # DQ-001: silent-truncation fix. The displayed content is the
    # post-cap text; these fields surface the cut state (was the
    # original truncated?) and the original (pre-cap) character length
    # so callers can detect the cut and fetch the full body via
    # ``evidence_handle`` (namespace/object_id).
    content_truncated: bool = False
    content_length: int | None = None


class ContextPackGroup(BaseModel):
    title: str
    items: list[ContextPackItem]


class ContextPack(BaseModel):
    mode: ContextMode
    query_text: str
    groups: list[ContextPackGroup]
    max_chars: int
    used_chars: int
    suppressed: dict[str, int] = Field(default_factory=dict)
    #: RET-007 — additive, default-empty bounded degradation codes threaded from the retrieval
    #: envelope. A healthy pack carries ``[]``.
    warnings: list[str] = Field(default_factory=list)


class _RankedCandidate(BaseModel):
    candidate: ContextCandidate
    kind: EssenceKind
    staleness: StalenessTier
    bm25: float
    token_overlap: int
    score: float


def build_context_pack(
    candidates: list[ContextCandidate],
    query: ContextPackQuery,
    warnings: list[str] | None = None,
) -> ContextPack:
    """Return a grouped, char-capped essence context pack."""

    visible, suppressed = _visible_candidates(candidates, include_history=query.include_history)
    ranked = _rank(visible, query)

    recent_pool = sorted(
        [r for r in ranked if r.candidate.lane == "recent"],
        key=lambda r: r.candidate.created_epoch,
        reverse=True,
    )
    ranked_pool = [r for r in ranked if r.candidate.lane == "ranked"]

    groups: dict[str, list[ContextPackItem]] = {title: [] for title in _GROUP_ORDER}
    used_chars = 0
    used_items = 0
    query_tokens = _tokenize(query.query_text)

    if len(recent_pool) > 0 and len(ranked_pool) > 0 and query.max_items >= 2:
        recent_max_chars = query.max_chars // 3
    else:
        recent_max_chars = query.max_chars

    # 1. Fill recent quota. If ranked has no candidates, let recent use the
    # entire item budget rather than suppressing valid context behind a reserve.
    recent_item_limit = query.recent_reserve if ranked_pool else query.max_items
    for r in recent_pool:
        if used_items >= recent_item_limit or used_items >= query.max_items:
            break
        item = _to_item(r, remaining_chars=recent_max_chars - used_chars)
        if item is None:
            break
        groups[_GROUP_BY_KIND[item.kind]].append(item)
        used_items += 1
        used_chars += len(item.content)
        if used_chars >= query.max_chars:
            break

    # 2. Fill remaining from ranked pool
    for r in ranked_pool:
        if used_items >= query.max_items:
            break
        if _should_skip_ranked_filler(r, has_query=bool(query_tokens)):
            suppressed["low_relevance"] = suppressed.get("low_relevance", 0) + 1
            continue
        item = _to_item(r, remaining_chars=query.max_chars - used_chars)
        if item is None:
            break
        groups[_GROUP_BY_KIND[item.kind]].append(item)
        used_items += 1
        used_chars += len(item.content)
        if used_chars >= query.max_chars:
            break

    packed_groups = [
        ContextPackGroup(title=title, items=items)
        for title in _GROUP_ORDER
        if (items := groups[title])
    ]
    return ContextPack(
        mode=query.mode,
        query_text=query.query_text,
        groups=packed_groups,
        max_chars=query.max_chars,
        used_chars=used_chars,
        suppressed=suppressed,
        warnings=list(warnings) if warnings else [],
    )


def render_context_pack_text(pack: ContextPack) -> str:
    """Render a pack as prompt-ready text."""

    lines: list[str] = []
    for group in pack.groups:
        lines.append(f"{group.title}:")
        for item in group.items:
            lines.append(f"- [{item.kind}; {item.evidence_handle}] {item.content}")
    return "\n".join(lines)


def _visible_candidates(
    candidates: list[ContextCandidate],
    *,
    include_history: bool,
) -> tuple[list[ContextCandidate], dict[str, int]]:
    visible: list[ContextCandidate] = []
    suppressed: dict[str, int] = {}
    for candidate in candidates:
        staleness = _staleness_of(candidate)
        kind = _kind_of(candidate)
        if not include_history and (staleness == "superseded" or candidate.state == "superseded"):
            suppressed["superseded"] = suppressed.get("superseded", 0) + 1
            continue
        if not include_history and kind == "correction/suppression":
            suppressed["correction/suppression"] = suppressed.get("correction/suppression", 0) + 1
            continue
        visible.append(candidate)
    return visible, suppressed


def _rank(candidates: list[ContextCandidate], query: ContextPackQuery) -> list[_RankedCandidate]:
    query_tokens = _tokenize(query.query_text)
    documents = [_tokenize(_candidate_text(candidate)) for candidate in candidates]
    bm25_scores = _bm25(query_tokens, documents)
    max_bm25 = max(bm25_scores, default=0.0)
    normalizer = max_bm25 if max_bm25 > 0 else 1.0
    ranked: list[_RankedCandidate] = []

    for candidate, doc_tokens, bm25 in zip(candidates, documents, bm25_scores, strict=True):
        kind = _kind_of(candidate)
        staleness = _staleness_of(candidate)
        overlap = len(set(query_tokens) & set(doc_tokens)) if query_tokens else 0
        importance = min(10, max(1, candidate.importance)) / 10.0
        recency = _recency_hint(candidate)
        relevance = bm25 / normalizer
        score = (
            _KIND_PRIORITY[kind]
            + _STALENESS_PRIORITY[staleness]
            + (2.0 * relevance)
            + (0.6 * min(1.0, max(0.0, candidate.retrieve_score)))
            + (0.5 * importance)
            + recency
        )
        if query_tokens and overlap == 0 and staleness not in ("durable", "current"):
            score -= 2.0
        ranked.append(
            _RankedCandidate(
                candidate=candidate,
                kind=kind,
                staleness=staleness,
                bm25=bm25,
                token_overlap=overlap,
                score=score,
            )
        )

    return sorted(
        ranked,
        key=lambda row: (
            row.score,
            _STALENESS_PRIORITY[row.staleness],
            _KIND_PRIORITY[row.kind],
            row.candidate.created_epoch,
        ),
        reverse=True,
    )


def _to_item(ranked: _RankedCandidate, *, remaining_chars: int) -> ContextPackItem | None:
    if remaining_chars <= 0:
        return None
    text = _display_text(ranked.candidate)
    # DQ-001: `_cap_text` normalizes whitespace before applying its cap. Measure the same
    # normalized display text so whitespace collapse cannot produce a false truncation signal.
    display_text = " ".join(text.split())
    original_length = len(display_text)
    from musubi.retrieve.grapheme_truncation import truncate_grapheme_safe

    content = truncate_grapheme_safe(display_text, max_chars=remaining_chars, suffix="...")
    if not content:
        return None
    evidence = f"{ranked.candidate.namespace}/{ranked.candidate.object_id}"
    return ContextPackItem(
        object_id=ranked.candidate.object_id,
        namespace=ranked.candidate.namespace,
        plane=ranked.candidate.plane,
        kind=ranked.kind,
        staleness=ranked.staleness,
        content=content,
        evidence_handle=evidence,
        why_surfaced=_why(ranked),
        score=round(ranked.score, 4),
        # DQ-001: silent-truncation fix. Was the displayed text cut from a
        # longer original? Default False (no cut); set True when the cap
        # truncated the text. Carry the original length alongside so
        # callers can detect the cut and fetch the full body.
        content_truncated=(original_length > remaining_chars),
        content_length=original_length,
    )


def _should_skip_ranked_filler(ranked: _RankedCandidate, *, has_query: bool) -> bool:
    """Avoid stuffing startup packs with unrelated episodic residue."""
    if not has_query:
        return False
    if ranked.token_overlap > 0:
        return False
    if ranked.staleness in ("durable", "current"):
        return False
    return ranked.kind == "episode"


def _candidate_text(candidate: ContextCandidate) -> str:
    parts = [
        candidate.title or "",
        candidate.summary or "",
        candidate.content,
        " ".join(candidate.tags),
    ]
    return " ".join(part for part in parts if part)


def _display_text(candidate: ContextCandidate) -> str:
    return (candidate.summary or candidate.content).strip()


def _kind_of(candidate: ContextCandidate) -> EssenceKind:
    raw = _metadata_value(candidate, "kind")
    if raw in VALID_KINDS:
        return raw  # type: ignore[return-value]
    return "episode"


def _staleness_of(candidate: ContextCandidate) -> StalenessTier:
    raw = _metadata_value(candidate, "staleness") or _metadata_value(candidate, "freshness")
    if raw in VALID_STALENESS:
        return raw  # type: ignore[return-value]
    if candidate.state == "superseded":
        return "superseded"
    if candidate.state in ("archived", "demoted"):
        return "episodic"
    return "episodic"


def _metadata_value(candidate: ContextCandidate, key: str) -> str | None:
    extra_value = candidate.extra.get(key)
    if isinstance(extra_value, str):
        return extra_value.strip()
    prefix = f"{key}:"
    for tag in candidate.tags:
        if tag.startswith(prefix):
            return tag[len(prefix) :].strip()
    return None


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text)]


def _bm25(query_tokens: list[str], documents: list[list[str]]) -> list[float]:
    if not query_tokens or not documents:
        return [0.0 for _ in documents]

    doc_freq: Counter[str] = Counter()
    for doc in documents:
        doc_freq.update(set(doc))
    avg_len = sum(len(doc) for doc in documents) / max(1, len(documents))
    k1 = 1.5
    b = 0.75
    scores: list[float] = []

    for doc in documents:
        counts = Counter(doc)
        doc_len = len(doc) or 1
        score = 0.0
        for token in query_tokens:
            freq = counts[token]
            if freq == 0:
                continue
            idf = math.log(1 + (len(documents) - doc_freq[token] + 0.5) / (doc_freq[token] + 0.5))
            denom = freq + k1 * (1 - b + b * (doc_len / max(avg_len, 1.0)))
            score += idf * ((freq * (k1 + 1)) / denom)
        scores.append(score)
    return scores


def _recency_hint(candidate: ContextCandidate) -> float:
    epoch = (
        candidate.updated_epoch if candidate.updated_epoch is not None else candidate.created_epoch
    )
    if epoch <= 0:
        return 0.0
    # Only a tiebreaker: newer records should not outrank durable rules
    # solely by being fresh.
    return min(0.5, math.log10(max(epoch, 1.0)) / 20.0)


def _why(ranked: _RankedCandidate) -> str:
    fragments: list[str] = []
    if ranked.staleness == "durable":
        fragments.append(f"durable {ranked.kind}")
    elif ranked.staleness == "current":
        fragments.append(f"current {ranked.kind}")
    else:
        fragments.append(ranked.kind)
    if ranked.token_overlap:
        fragments.append(f"{ranked.token_overlap} query-token matches")
    elif ranked.staleness == "durable":
        fragments.append("durable-vs-overlap tiebreak")
    if ranked.bm25 > 0:
        fragments.append("BM25 lexical match")
    return "; ".join(fragments)


__all__ = [
    "VALID_KINDS",
    "VALID_STALENESS",
    "ContextCandidate",
    "ContextMode",
    "ContextPack",
    "ContextPackGroup",
    "ContextPackItem",
    "ContextPackQuery",
    "EssenceKind",
    "StalenessTier",
    "build_context_pack",
    "render_context_pack_text",
]
