"""Test contract — slice-ingestion-capture bullet 22 (100-item batch
capture under 1s end-to-end against the live stack).

Closes Issue #118 (followup to slice-ops-integration-harness PR #114).

The original placeholder lived in ``tests/ingestion/test_capture.py``
as ``test_batch_capture_100_items_under_1s`` skipped with
"deferred to a follow-up perf suite." The harness landed in PR #114
and the production bootstrap landed in PR #126; this is the
consumer-side unskip per the canonical pattern.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.integration


def _strict_perf_budgets() -> bool:
    """Same env-var gate as ``tests/integration/test_smoke.py`` —
    on the CPU-only test stack, 100-item batch under 1s is
    unrealistic; gate the strict assertion on operator's nightly
    GPU host where the spec budget actually holds."""
    return os.environ.get("MUSUBI_TEST_PERF_BUDGETS", "").lower() == "strict"


@pytest.mark.skipif(
    not _strict_perf_budgets(),
    reason=(
        "perf budget is CPU-stack-unrealistic; set "
        "MUSUBI_TEST_PERF_BUDGETS=strict on a GPU reference host "
        "(operator's nightly runner) to enforce. CPU-stack runs still "
        "exercise the path; only the wall-clock assertion is gated."
    ),
)
async def test_batch_capture_100_items_under_1s(api_client: Any) -> None:
    """Bullet 22 — single-batch capture of 100 items completes
    end-to-end (one TEI batch embed + one Qdrant upsert + the API
    routing overhead) under the spec's 1s budget."""
    namespace = "eric/integration-test/episodic"
    items = [
        f"batch-perf-{uuid.uuid4().hex[:6]}-{i}-payload-some-distinct-content" for i in range(100)
    ]

    start = time.monotonic()
    async with api_client.episodic.batch(namespace=namespace) as batch:
        for content in items:
            batch.capture(content=content, importance=4)
    elapsed = time.monotonic() - start

    assert batch.results is not None, "batch context did not flush"
    assert elapsed < 1.0, f"100-item batch capture took {elapsed * 1000:.0f}ms (budget 1000ms)"


async def test_batch_capture_100_items_completes_without_strict_budget(
    api_client: Any,
) -> None:
    """Surface-form companion that runs on the CPU stack too — verifies
    the path executes correctly + the batch flushes its single POST,
    without asserting the spec's 1s wall-clock. Strict budget lives in
    the test above."""
    namespace = "eric/integration-test/episodic"
    items = [f"batch-surface-{uuid.uuid4().hex[:6]}-{i}-payload" for i in range(100)]

    async with api_client.episodic.batch(namespace=namespace) as batch:
        for content in items:
            batch.capture(content=content, importance=4)

    assert batch.results is not None
    # Batch endpoint returns either {"items": [...]} or {"object_ids": [...]}
    # depending on the API version; tolerate either shape.
    flushed = batch.results
    if "items" in flushed:
        assert len(flushed["items"]) == 100
    elif "object_ids" in flushed:
        assert len(flushed["object_ids"]) == 100
