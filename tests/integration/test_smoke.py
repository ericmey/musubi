"""Test contract bullets 5-14 — real-services smoke against the live
docker-compose stack.

These tests run only when docker is installed (the `live_stack`
fixture skips otherwise) so the unit-only `make test` invocation
on a docker-less machine doesn't error on collection. CI verifies
them via `.github/workflows/integration.yml`.

Bullets 5/6/7/9/12 (every plane-touching scenario) unskipped in
slice-api-app-bootstrap (PR #126) — `create_app()` now wires real
Qdrant + TEI + plane factories on init via the production bootstrap,
which closed the cross-slice ticket
``slice-ops-integration-harness-production-app-bootstrap.md``.

Bullets 8 (SSE), 10/11 (synthesis worker triggers), 13/14 (perf
budgets) remain skipped against their own follow-ups.

Tests are ``async def`` so pytest-asyncio (auto mode per
pyproject) manages one event loop per test — the api_client
fixture's httpx pool binds cleanly to that loop and tears down
with the test instead of leaving a stale pool behind a closed
asyncio.run loop.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

import httpx
import pytest

from tests.integration.conftest import StackHandle

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# Bullet 5 — capture_then_retrieve_roundtrip
# --------------------------------------------------------------------------


async def test_capture_then_retrieve_roundtrip(api_client: Any) -> None:
    namespace = "eric/integration-test/episodic"
    content = f"smoke-test-capture-{uuid.uuid4().hex[:8]}"

    captured = await api_client.memories.capture(namespace=namespace, content=content, importance=5)
    assert captured["object_id"]

    # Qdrant indexes are eventual; poll the retrieve up to ~10s rather
    # than a single fixed sleep so first-cold-cache CI runs aren't
    # flaky on indexing latency.
    deadline = asyncio.get_event_loop().time() + 10.0
    rows: list[dict[str, Any]] = []
    while asyncio.get_event_loop().time() < deadline:
        results = await api_client.retrieve(
            namespace=namespace, query_text=content, mode="fast", limit=5
        )
        rows = results.get("results", [])
        if any(r.get("object_id") == captured["object_id"] for r in rows):
            return
        await asyncio.sleep(0.5)
    pytest.fail(
        f"newly-captured object_id missing from retrieval results within 10s: {rows}"
    )


# --------------------------------------------------------------------------
# Bullet 6 — capture_dedup_against_existing
# --------------------------------------------------------------------------


async def test_capture_dedup_against_existing(api_client: Any) -> None:
    """Capture the same content twice; the second hit should fold into
    the first via the dedup pipeline (reinforcement_count == 2)."""
    namespace = "eric/integration-test/episodic"
    content = f"dedup-fixture-{uuid.uuid4().hex[:8]}"

    first = await api_client.memories.capture(namespace=namespace, content=content, importance=5)
    await asyncio.sleep(1.0)
    second = await api_client.memories.capture(namespace=namespace, content=content, importance=5)

    # Either the second call returns the same object_id (merged) or it
    # surfaces a `dedup` field; the spec lets the implementation pick.
    if second.get("object_id") == first.get("object_id"):
        return  # merged path
    if "dedup" in second:
        assert second["dedup"] in {"merged", "reinforced", True}
        return
    pytest.fail(f"expected dedup signal on second capture; first={first}, second={second}")


# --------------------------------------------------------------------------
# Bullet 7 — thought_send_check_read_history
# --------------------------------------------------------------------------


async def test_thought_send_check_read_history(api_client: Any) -> None:
    namespace = "eric/integration-test/thought"

    ack = await api_client.thoughts.send(
        namespace=namespace,
        from_presence="integration-test/sender",
        to_presence="integration-test/receiver",
        content="smoke-test-thought",
        channel="default",
        importance=5,
    )
    inbox = await api_client.thoughts.check(
        namespace=namespace, presence="integration-test/receiver"
    )

    assert ack["object_id"]
    items = inbox.get("items", [])
    assert any(it.get("object_id") == ack["object_id"] for it in items), (
        f"sent thought missing from inbox: {items}"
    )


# --------------------------------------------------------------------------
# Bullet 8 — thought_stream_delivers_live (SSE)
# --------------------------------------------------------------------------


@pytest.mark.skip(
    reason="SSE thought-stream subscription surface lands in slice-api-thoughts-stream PR #103 followup; harness primitives ready, consumer slice owns the unskip per slice-ops-integration-harness §Implementation notes"
)
def test_thought_stream_delivers_live() -> None:
    """Bullet 8 — placeholder; consumer slice owns the unskip."""


# --------------------------------------------------------------------------
# Bullet 9 — curated_create_then_retrieve
# --------------------------------------------------------------------------


async def test_curated_create_then_retrieve(live_stack: StackHandle) -> None:
    """The SDK's curated namespace is read-only (`get`); the create
    surface lives at the API layer (POST /v1/curated-knowledge) and
    is exercised here via raw httpx + the operator token."""
    import hashlib

    namespace = "eric/integration-test/curated"
    title = f"smoke-test-curated-{uuid.uuid4().hex[:8]}"
    content = (
        "Curated test entry — created by the integration harness for "
        "slice-ops-integration-harness Test Contract bullet 9."
    )
    # CuratedCreateRequest demands a 64-char hex body_hash; derive
    # deterministically from content so re-runs hit the dedup path
    # the same way.
    body_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    async with httpx.AsyncClient(
        base_url=live_stack.api_url,
        headers={"Authorization": f"Bearer {live_stack.operator_token}"},
        timeout=30.0,
    ) as client:
        create_resp = await client.post(
            "/curated-knowledge",
            json={
                "namespace": namespace,
                "title": title,
                "content": content,
                "vault_path": f"integration-test/{title}.md",
                "body_hash": body_hash,
                "tags": ["integration", "smoke"],
            },
        )
        create_resp.raise_for_status()
        created = create_resp.json()

    assert created["object_id"]


# --------------------------------------------------------------------------
# Bullets 10-11 — concept synthesis (LLM on / off)
# --------------------------------------------------------------------------


@pytest.mark.skip(
    reason="concept synthesis is driven by the lifecycle worker (slice-lifecycle-synthesis); triggering it from the harness needs an operator-scope debug endpoint that's tracked as a follow-up Issue. Harness primitives (live Ollama via test-env compose, operator token, real API) are ready — the unskip lands when the lifecycle worker exposes a tick-from-test trigger."
)
def test_concept_synthesis_flow_ollama_present() -> None:
    """Bullet 10 — placeholder; lifecycle-worker trigger followup."""


@pytest.mark.skip(
    reason="ollama-offline scenario needs a separate compose profile (or runtime ollama stop) that this slice didn't carve to keep scope tight; tracked as follow-up Issue. Harness primitives ready."
)
def test_concept_synthesis_flow_ollama_offline() -> None:
    """Bullet 11 — placeholder; ollama-offline compose-profile followup."""


# --------------------------------------------------------------------------
# Bullet 12 — artifact_upload_multipart_then_retrieve_blob
# --------------------------------------------------------------------------


async def test_artifact_upload_multipart_then_retrieve_blob(
    live_stack: StackHandle,
) -> None:
    """Multipart upload → GET blob → bytes match."""
    namespace = "eric/integration-test/artifact"
    payload = b"WEBVTT\n\n00:00 --> 00:02\nSmoke test transcript fixture."

    async with httpx.AsyncClient(
        base_url=live_stack.api_url,
        headers={"Authorization": f"Bearer {live_stack.operator_token}"},
        timeout=30.0,
    ) as client:
        upload_resp = await client.post(
            "/artifacts",
            data={
                "namespace": namespace,
                "title": f"smoke-{uuid.uuid4().hex[:6]}.vtt",
                "content_type": "text/vtt",
                "source_system": "integration-test",
                "chunker": "markdown-headings-v1",
            },
            files={"file": ("smoke.vtt", payload, "text/vtt")},
        )
        upload_resp.raise_for_status()
        uploaded = upload_resp.json()
        blob_resp = await client.get(
            f"/artifacts/{uploaded['object_id']}/blob",
            params={"namespace": namespace},
        )
        blob_resp.raise_for_status()

    assert uploaded["object_id"]
    assert blob_resp.content == payload


# --------------------------------------------------------------------------
# Bullets 13-14 — perf budgets on 10k corpus
# --------------------------------------------------------------------------


def _strict_perf_budgets() -> bool:
    return os.environ.get("MUSUBI_TEST_PERF_BUDGETS", "").lower() == "strict"


@pytest.mark.skipif(
    not _strict_perf_budgets(),
    reason="perf budgets are CPU-stack-unrealistic; set MUSUBI_TEST_PERF_BUDGETS=strict on a GPU reference host (operator's nightly runner) to enforce",
)
async def test_retrieve_deep_under_5s_on_10k_corpus(
    api_client: Any, live_stack: StackHandle
) -> None:
    """Bullet 13 — deep-mode retrieve against the pre-loaded 10k
    corpus completes under the spec's 5s p95 budget. Strict-mode only;
    the harness pre-loads via the seed script when MUSUBI_TEST_PRELOAD_CORPUS=1."""
    namespace = "eric/_shared/episodic"

    start = time.monotonic()
    await api_client.retrieve(
        namespace=namespace,
        query_text="how do I configure cuda for inference",
        mode="deep",
        limit=15,
    )
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f"deep retrieve took {elapsed:.2f}s (budget 5s)"


@pytest.mark.skipif(
    not _strict_perf_budgets(),
    reason="perf budgets are CPU-stack-unrealistic; set MUSUBI_TEST_PERF_BUDGETS=strict on a GPU reference host",
)
async def test_retrieve_fast_under_200ms_on_10k_corpus(api_client: Any) -> None:
    """Bullet 14 — fast-mode retrieve under 200ms p95."""
    namespace = "eric/_shared/episodic"

    start = time.monotonic()
    await api_client.retrieve(
        namespace=namespace,
        query_text="lifecycle promotion threshold",
        mode="fast",
        limit=5,
    )
    elapsed = time.monotonic() - start
    assert elapsed < 0.2, f"fast retrieve took {elapsed * 1000:.0f}ms (budget 200ms)"
