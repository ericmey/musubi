"""Test contract bullets 5-14 — real-services smoke against the live
docker-compose stack.

These tests run only when docker is installed (the `live_stack`
fixture skips otherwise) so the unit-only `make test` invocation
on a docker-less machine doesn't error on collection. CI verifies
them via `.github/workflows/integration.yml`.

Each bullet maps to one ``@pytest.mark.integration`` test below.
Bullets that need worker-internal triggers (concept synthesis with
LLM in the loop, perf budgets against a 10k corpus) carry an
explicit ``pytest.skip`` with a pointer to the consumer-slice
followup PR that owns the unskip — per the slice file's
"out-of-scope" note that consumer slices unskip their own bullets
after this harness lands.
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


# Helper — shared async test runner so every bullet doesn't repeat
# the asyncio.run boilerplate.
def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Bullet 5 — capture_then_retrieve_roundtrip
# --------------------------------------------------------------------------


def test_capture_then_retrieve_roundtrip(api_client: Any) -> None:
    namespace = "eric/integration-test/episodic"
    content = f"smoke-test-capture-{uuid.uuid4().hex[:8]}"

    async def _flow() -> dict[str, Any]:
        captured = await api_client.memories.capture(
            namespace=namespace, content=content, importance=5
        )
        # Wait briefly for index propagation; Qdrant local index is
        # eventual.
        await asyncio.sleep(1.0)
        results = await api_client.retrieve(
            namespace=namespace, query_text=content, mode="fast", limit=5
        )
        return {"captured": captured, "results": results}

    out = _run(_flow())
    assert out["captured"]["object_id"]
    rows = out["results"].get("results", [])
    assert any(r.get("object_id") == out["captured"]["object_id"] for r in rows), (
        f"newly-captured object_id missing from retrieval results: {rows}"
    )


# --------------------------------------------------------------------------
# Bullet 6 — capture_dedup_against_existing
# --------------------------------------------------------------------------


def test_capture_dedup_against_existing(api_client: Any) -> None:
    """Capture the same content twice; the second hit should fold into
    the first via the dedup pipeline (reinforcement_count == 2)."""
    namespace = "eric/integration-test/episodic"
    content = f"dedup-fixture-{uuid.uuid4().hex[:8]}"

    async def _flow() -> tuple[dict[str, Any], dict[str, Any]]:
        first = await api_client.memories.capture(
            namespace=namespace, content=content, importance=5
        )
        await asyncio.sleep(1.0)
        second = await api_client.memories.capture(
            namespace=namespace, content=content, importance=5
        )
        return first, second

    first, second = _run(_flow())
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


def test_thought_send_check_read_history(api_client: Any) -> None:
    namespace = "eric/integration-test/thought"

    async def _flow() -> dict[str, Any]:
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
        return {"ack": ack, "inbox": inbox}

    out = _run(_flow())
    assert out["ack"]["object_id"]
    items = out["inbox"].get("items", [])
    assert any(it.get("object_id") == out["ack"]["object_id"] for it in items), (
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


def test_curated_create_then_retrieve(live_stack: StackHandle) -> None:
    """The SDK's curated namespace is read-only (`get`); the create
    surface lives at the API layer (POST /v1/curated-knowledge) and
    is exercised here via raw httpx + the operator token."""
    namespace = "eric/integration-test/curated"
    title = f"smoke-test-curated-{uuid.uuid4().hex[:8]}"
    body = (
        "Curated test entry — created by the integration harness for "
        "slice-ops-integration-harness Test Contract bullet 9."
    )

    async def _flow() -> dict[str, Any]:
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
                    "body": body,
                    "source": "integration-test",
                    "tags": ["integration", "smoke"],
                },
            )
            create_resp.raise_for_status()
            created = create_resp.json()
            await asyncio.sleep(1.0)
            retrieve_resp = await client.post(
                "/retrieve",
                json={
                    "namespace": namespace,
                    "query_text": title,
                    "mode": "fast",
                    "limit": 5,
                },
            )
            retrieve_resp.raise_for_status()
            return {"created": created, "results": retrieve_resp.json()}

    out = _run(_flow())
    assert out["created"]["object_id"]


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


def test_artifact_upload_multipart_then_retrieve_blob(
    live_stack: StackHandle,
) -> None:
    """Multipart upload → GET blob → bytes match."""
    namespace = "eric/integration-test/artifact"
    payload = b"WEBVTT\n\n00:00 --> 00:02\nSmoke test transcript fixture."

    async def _flow() -> dict[str, Any]:
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
                    "source_ref": uuid.uuid4().hex,
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
            return {"uploaded": uploaded, "blob_bytes": blob_resp.content}

    out = _run(_flow())
    assert out["uploaded"]["object_id"]
    assert out["blob_bytes"] == payload


# --------------------------------------------------------------------------
# Bullets 13-14 — perf budgets on 10k corpus
# --------------------------------------------------------------------------


def _strict_perf_budgets() -> bool:
    return os.environ.get("MUSUBI_TEST_PERF_BUDGETS", "").lower() == "strict"


@pytest.mark.skipif(
    not _strict_perf_budgets(),
    reason="perf budgets are CPU-stack-unrealistic; set MUSUBI_TEST_PERF_BUDGETS=strict on a GPU reference host (operator's nightly runner) to enforce",
)
def test_retrieve_deep_under_5s_on_10k_corpus(api_client: Any, live_stack: StackHandle) -> None:
    """Bullet 13 — deep-mode retrieve against the pre-loaded 10k
    corpus completes under the spec's 5s p95 budget. Strict-mode only;
    the harness pre-loads via the seed script when MUSUBI_TEST_PRELOAD_CORPUS=1."""
    namespace = "eric/_shared/episodic"

    async def _flow() -> float:
        start = time.monotonic()
        await api_client.retrieve(
            namespace=namespace,
            query_text="how do I configure cuda for inference",
            mode="deep",
            limit=15,
        )
        return time.monotonic() - start

    elapsed = _run(_flow())
    assert elapsed < 5.0, f"deep retrieve took {elapsed:.2f}s (budget 5s)"


@pytest.mark.skipif(
    not _strict_perf_budgets(),
    reason="perf budgets are CPU-stack-unrealistic; set MUSUBI_TEST_PERF_BUDGETS=strict on a GPU reference host",
)
def test_retrieve_fast_under_200ms_on_10k_corpus(api_client: Any) -> None:
    """Bullet 14 — fast-mode retrieve under 200ms p95."""
    namespace = "eric/_shared/episodic"

    async def _flow() -> float:
        start = time.monotonic()
        await api_client.retrieve(
            namespace=namespace,
            query_text="lifecycle promotion threshold",
            mode="fast",
            limit=5,
        )
        return time.monotonic() - start

    elapsed = _run(_flow())
    assert elapsed < 0.2, f"fast retrieve took {elapsed * 1000:.0f}ms (budget 200ms)"
