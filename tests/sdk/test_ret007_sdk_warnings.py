"""RET-007 — SDK warnings passthrough (PASS controls).

Owner slice: slice-ret007-degradation (Musubi SDK). Tests/docs only, no src.

Per contract §5 + Yua: the sync and async SDK ``retrieve`` already return the raw JSON ``dict`` and
therefore preserve a server-sent ``warnings`` key MECHANICALLY. These are ordinary green PASS
controls — they must keep holding after the fix. (No ``SDKResult.warnings`` typed field is added by
this slice.)

    uv run pytest tests/sdk/test_ret007_sdk_warnings.py -v
"""

import httpx

from musubi.sdk import AsyncMusubiClient, MusubiClient
from musubi.sdk.retry import RetryPolicy

_BASE = "https://musubi.test/v1"


def _handler_with_warnings(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/v1/retrieve"
    return httpx.Response(200, json={"results": [], "warnings": ["sparse_embedding_failed"]})


def test_sync_sdk_preserves_warnings() -> None:
    client = MusubiClient(
        base_url=_BASE,
        token="t",
        retry=RetryPolicy(max_attempts=1, base_backoff=0.0),
        transport=httpx.MockTransport(_handler_with_warnings),
    )
    res = client.retrieve(
        namespace="test/ns/episodic", query_text="q", mode="fast", planes=["episodic"]
    )
    assert isinstance(res, dict)
    assert res.get("warnings") == ["sparse_embedding_failed"], (
        "sync SDK must pass the raw warnings array through"
    )


async def test_async_sdk_preserves_warnings() -> None:
    async with AsyncMusubiClient(
        base_url=_BASE,
        token="t",
        retry=RetryPolicy(max_attempts=1, base_backoff=0.0),
        transport=httpx.MockTransport(_handler_with_warnings),
    ) as client:
        res = await client.retrieve(
            namespace="test/ns/episodic", query_text="q", mode="fast", planes=["episodic"]
        )
    assert isinstance(res, dict)
    assert res.get("warnings") == ["sparse_embedding_failed"], (
        "async SDK must pass the raw warnings array through"
    )
