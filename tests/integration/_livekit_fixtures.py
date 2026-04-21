"""Synthetic LiveKit event generators for the e2e integration tests.

The adapter responds to four LiveKit events (``transcript_segment_received``,
``on_user_turn_completed``, ``session_ends``, and an optional
``interesting_fact_detected`` heuristic). These helpers produce the
*shape* of those payloads so the e2e tests can drive the adapter
against a real Musubi stack without a running LiveKit SFU.

Kept deliberately small: the adapter tests are about the adapter <>
Musubi contract, not about simulating LiveKit's WebRTC wire format.
"""

from __future__ import annotations

import uuid


def progressive_segments(final_utterance: str, chunk_size: int = 3) -> list[str]:
    """Break a completed utterance into cumulative interim segments.

    Mirrors how LiveKit emits ``transcript_segment_received`` events:
    each event carries the full transcript-so-far, not a delta. With
    the default ``chunk_size=3`` a 7-word final utterance yields
    ``["w1 w2 w3", "w1 w2 w3 w4 w5 w6", "w1…w7"]`` — so the
    SlowThinker sees one cancel-restart tick per chunk boundary plus
    a final full-utterance tick.
    """
    words = final_utterance.split()
    if not words:
        return []
    segments: list[str] = []
    for cut in range(chunk_size, len(words) + 1, chunk_size):
        segments.append(" ".join(words[:cut]))
    if not segments or segments[-1] != final_utterance:
        segments.append(final_utterance)
    return segments


def minimal_vtt(utterance: str, *, speaker: str = "user") -> str:
    """A single-cue WEBVTT document the adapter's session-end path
    accepts. Real LiveKit transcripts carry multi-cue VTT; the
    adapter's upload path treats the body as opaque bytes, so a
    single cue is sufficient for the contract tests."""
    return f"WEBVTT\n\n00:00:00.000 --> 00:00:05.000\n<v {speaker}>{utterance}\n"


def new_session_id() -> str:
    """One helper so every test logs a unique session id into the
    persisted thought + artifact payload, and retries across runs
    don't collide on the server-side dedup index."""
    return f"e2e-session-{uuid.uuid4().hex[:8]}"


SAMPLE_INTERESTING_FACT = "Please remember that my calendar assistant is named Aoi."
"""An utterance that `detect_interesting_fact` matches (word
``remember``) so ``maybe_capture_fact`` writes to Musubi."""

SAMPLE_FILLER_PHRASE = "Uh, yeah, so anyway, you know, that's pretty much it I guess."
"""An utterance that `detect_interesting_fact` does NOT match. Any
capture here would mean the heuristic gate failed."""

SAMPLE_EMAIL_LEAK = "My email is alex.example@example.com if you want to forward it."
"""An utterance with an email that redact_pii should scrub before
the adapter uploads."""
