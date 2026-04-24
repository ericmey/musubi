"""Test contract for slice-adapter-livekit-e2e.

The unit suite (`tests/adapters/test_livekit.py`) exercises the adapter
against a :class:`AsyncFakeMusubiClient`; this module exercises it
against the live docker-compose stack via the ``api_client`` fixture.
The point isn't to re-prove every adapter code path — it's to prove the
*contract* between the adapter and the real :class:`AsyncMusubiClient`
→ real Musubi API → real Qdrant holds, so a silent drift in the SDK
shape (field rename, scope check added, idempotency-key header
renamed, …) is caught before it hits production.

Per the slice spec there are five bullets; each one builds a fresh
:class:`LiveKitAdapter` pointed at the live stack, drives synthetic
LiveKit events, then asserts against the live Musubi SDK.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import pytest

from musubi.adapters.livekit import LiveKitAdapter, LiveKitAdapterConfig
from tests.integration._livekit_fixtures import (
    SAMPLE_EMAIL_LEAK,
    SAMPLE_FILLER_PHRASE,
    SAMPLE_INTERESTING_FACT,
    minimal_vtt,
    new_session_id,
    progressive_segments,
)

pytestmark = pytest.mark.integration

# Both memories.capture and thoughts.send authorise via per-namespace
# scope-strings on the operator token minted in conftest. The router
# routes to the right Qdrant collection from the endpoint, not from
# the namespace string — so a single namespace covers both writes
# without needing a token rescope or a multi-plane fixture.
_NS = "eric/integration-test/episodic"
_ARTIFACT_NS = "eric/integration-test/artifact"


def _adapter(
    api_client: Any,
    *,
    capture_facts: bool = True,
    capture_transcripts: bool = True,
    redact_pii: bool = False,
) -> LiveKitAdapter:
    return LiveKitAdapter(
        client=api_client,
        namespace=_NS,
        artifact_namespace=_ARTIFACT_NS,
        config=LiveKitAdapterConfig(
            capture_facts=capture_facts,
            capture_transcripts=capture_transcripts,
            redact_pii=redact_pii,
            # Tight retrieval so slow-thinker pre-fetches don't burn
            # integration-test wall-clock.
            deep_limit=5,
            cache_default_ttl_s=30.0,
            upload_max_attempts=1,
            upload_backoff_s=0.0,
        ),
    )


async def _await_slow_thinker(adapter: LiveKitAdapter) -> None:
    """SlowThinker runs on a detached asyncio task; give the final
    pre-fetch a chance to settle before the test tears down so the
    test body's assertions don't race with an in-flight retrieve."""
    task = adapter.slow_thinker._task
    if task is not None and not task.done():
        # Only suppress the two "expected-quiet" outcomes: the wait
        # timed out (10s slack — SlowThinker should have settled), or
        # a newer segment cancelled it. Any other exception is a real
        # regression in the adapter / SDK and must surface.
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10.0)


# --------------------------------------------------------------------------
# Bullet 1 — full turn persists episodic + thought
# --------------------------------------------------------------------------


async def test_e2e_full_turn_persists_episodic_and_thought(api_client: Any) -> None:
    """A synthetic transcript sequence (interim → final → session end)
    against live Musubi must produce at least one episodic row (from
    ``maybe_capture_fact``) and one retrievable thought (from
    ``_send_session_thought``)."""
    adapter = _adapter(api_client)
    session_id = new_session_id()
    utterance = f"{SAMPLE_INTERESTING_FACT} ({session_id})"

    capture_responses: list[dict[str, Any]] = []
    original = api_client.episodic.capture

    async def capture_and_record(**kwargs: Any) -> Any:
        resp = await original(**kwargs)
        capture_responses.append(resp)
        return resp

    api_client.episodic.capture = capture_and_record
    try:
        for seg in progressive_segments(utterance):
            await adapter.on_transcript_segment(seg)
        await adapter.on_user_turn_completed(utterance)
        await adapter.maybe_capture_fact(utterance)
        await adapter.on_session_end(session_id=session_id, vtt_transcript=minimal_vtt(utterance))
        await _await_slow_thinker(adapter)
    finally:
        api_client.episodic.capture = original

    # Episodic half: at least one capture ack with a real object_id.
    # Includes both the heuristic fact capture and the session-end
    # transcript fallback (_upload_transcript_with_retry currently
    # routes through memories.capture until the SDK ships
    # artifacts.upload).
    assert capture_responses, "expected at least one episodic capture"
    assert all(resp.get("object_id") for resp in capture_responses)

    # Thought half: the adapter sends a session-end thought; pull the
    # namespace inbox and find it by its content body.
    inbox = await api_client.thoughts.check(namespace=_NS, presence="all")
    items = inbox.get("items", [])
    assert any(session_id in (item.get("content") or "") for item in items), (
        f"session-end thought missing from namespace inbox: {items}"
    )


# --------------------------------------------------------------------------
# Bullet 2 — redact_pii strips email before capture
# --------------------------------------------------------------------------


async def test_e2e_redaction_strips_email_before_capture(api_client: Any) -> None:
    """With ``redact_pii=True``, an email address in the utterance must
    not appear in the persisted episodic content."""
    adapter = _adapter(api_client, redact_pii=True)
    marker = uuid.uuid4().hex[:8]
    utterance = f"Please remember this ({marker}) — {SAMPLE_EMAIL_LEAK}"

    calls: list[dict[str, Any]] = []
    original = api_client.episodic.capture

    async def capture_and_record(**kwargs: Any) -> Any:
        # Record only after the real call succeeds — `maybe_capture_fact`
        # swallows `MusubiError`, so a record-before-await pattern could
        # green the test even when the server rejected the write.
        resp = await original(**kwargs)
        calls.append(kwargs)
        return resp

    api_client.episodic.capture = capture_and_record
    try:
        await adapter.maybe_capture_fact(utterance)
    finally:
        api_client.episodic.capture = original

    assert len(calls) == 1, (
        "maybe_capture_fact should have fired once (and the server must have accepted it)"
    )
    captured_content = calls[0]["content"]
    assert "alex.example@example.com" not in captured_content
    assert "[REDACTED]" in captured_content
    assert marker in captured_content, (
        "the redactor should only scrub PII, not the surrounding text"
    )


# --------------------------------------------------------------------------
# Bullet 3 — capture-side dedup collapses duplicate facts
# --------------------------------------------------------------------------


async def test_e2e_capture_side_dedup_collapses_duplicate_facts(api_client: Any) -> None:
    """Two identical ``maybe_capture_fact`` calls in quick succession
    produce one persisted row. Musubi's capture pipeline either merges
    (same object_id) or returns a ``dedup`` signal on the second hit —
    either shape is evidence of the dedup path firing.

    Note: the ``ContextCache`` in the adapter is a retrieve cache, not
    a capture gate; this bullet validates Musubi's capture-side dedup
    through the live adapter, per the slice spec's adjusted wording."""
    adapter = _adapter(api_client)
    marker = uuid.uuid4().hex[:8]
    utterance = f"Please remember the dedup marker is {marker}."

    # Capturing wrapper: a plain list + a pass-through coroutine holds
    # both the real server response and the call count without the
    # AsyncMock gymnastics.
    responses: list[dict[str, Any]] = []
    original = api_client.episodic.capture

    async def capture_and_record(**kwargs: Any) -> Any:
        resp = await original(**kwargs)
        responses.append(resp)
        return resp

    api_client.episodic.capture = capture_and_record
    try:
        await adapter.maybe_capture_fact(utterance)
        # 1.0s matches the smoke-test pattern in
        # test_capture_dedup_against_existing — enough slack for
        # Qdrant local-mode indexing to make the first row visible
        # to the second capture's dedup lookup.
        await asyncio.sleep(1.0)
        await adapter.maybe_capture_fact(utterance)
    finally:
        api_client.episodic.capture = original

    assert len(responses) == 2, (
        "adapter should have called episodic.capture twice; the server is "
        "responsible for dedup, not the adapter"
    )
    first_resp, second_resp = responses
    merged = second_resp.get("object_id") == first_resp.get("object_id")
    deduped = "dedup" in second_resp
    assert merged or deduped, (
        f"expected capture-side dedup: first={first_resp}, second={second_resp}"
    )


# --------------------------------------------------------------------------
# Bullet 4 — filler phrase does not capture
# --------------------------------------------------------------------------


async def test_e2e_filler_phrase_does_not_capture(api_client: Any) -> None:
    """An utterance that doesn't match ``detect_interesting_fact`` must
    not issue a capture call to Musubi. Unit tests cover this against
    FakeMusubiClient; the e2e form confirms the heuristic short-circuit
    still holds once the adapter is talking to a real SDK."""
    adapter = _adapter(api_client)
    calls: list[dict[str, Any]] = []
    original = api_client.episodic.capture

    async def capture_and_record(**kwargs: Any) -> Any:
        resp = await original(**kwargs)
        calls.append(kwargs)
        return resp

    api_client.episodic.capture = capture_and_record
    try:
        await adapter.maybe_capture_fact(SAMPLE_FILLER_PHRASE)
    finally:
        api_client.episodic.capture = original

    assert len(calls) == 0, (
        f"filler utterance should not trigger a capture; got "
        f"{len(calls)} call(s) with content {[c.get('content') for c in calls]}"
    )


# --------------------------------------------------------------------------
# Bullet 5 — session_end emits a retrievable thought
# --------------------------------------------------------------------------


async def test_e2e_session_end_emits_retrievable_thought(api_client: Any) -> None:
    """``on_session_end`` writes a summary thought that the thoughts
    plane surfaces via ``thoughts.check``. Proves the session-summary
    write path is wired end-to-end."""
    adapter = _adapter(api_client)
    session_id = new_session_id()

    await adapter.on_session_end(
        session_id=session_id,
        vtt_transcript=minimal_vtt(f"Short session marker {session_id}."),
    )

    inbox = await api_client.thoughts.check(namespace=_NS, presence="all")
    items = inbox.get("items", [])
    matches = [
        item
        for item in items
        if session_id in (item.get("content") or "")
        and (item.get("from_presence") or "") == "livekit-voice"
    ]
    assert matches, (
        f"session-end summary thought not found in namespace inbox for "
        f"session {session_id!r}: {items}"
    )
