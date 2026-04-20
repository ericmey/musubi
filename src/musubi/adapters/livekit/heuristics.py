"""Cheap heuristics for opportunistic memory capture during voice sessions.

Per [[07-interfaces/livekit-adapter]] § Event mapping —
``interesting_fact_detected`` is described as optional and pattern-based.
Keeping it as a small, explicit regex set lets the adapter capture
"remember…" / "I always forget…" style asides without needing an
LLM judge in the speech loop.
"""

from __future__ import annotations

import re

_INTERESTING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bremember\b", re.IGNORECASE),
    re.compile(r"\bI (?:always )?forget\b", re.IGNORECASE),
    re.compile(r"\b(?:save|store|note) (?:this|that) (?:to|in) memory\b", re.IGNORECASE),
    re.compile(r"\bnever forget\b", re.IGNORECASE),
)


def detect_interesting_fact(utterance: str) -> bool:
    """True if the utterance matches any of the heuristic patterns."""
    return any(p.search(utterance) for p in _INTERESTING_PATTERNS)
