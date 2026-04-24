"""Async :class:`AsyncMusubiClient` wrapping ``httpx.AsyncClient``.

Mirrors :mod:`musubi.sdk.client` function-for-function over an async
transport. Resource namespaces (``episodic``, ``curated``, ``concepts``,
``artifacts``, ``thoughts``, ``lifecycle``, ``ops``) hang off the client
instance per the spec; ``retrieve`` / ``retrieve_stream`` /
``probe_version`` live directly on the client. Caller is expected to use
``async with AsyncMusubiClient(...) as c:`` (closes the underlying
``httpx.AsyncClient`` on exit) — the sync variant uses the same shape.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx

from musubi.sdk.client import (
    _BEARER_HEADER,
    _IDEMPOTENCY_HEADER,
    _MIN_CORE_VERSION,
    _REQUEST_ID_HEADER,
    _ensure_tz_aware_created_at,
    _is_older,
    _retry_after_from,
)
from musubi.sdk.exceptions import (
    MusubiError,
    NetworkError,
    exception_for_status,
)
from musubi.sdk.result import SDKResult
from musubi.sdk.retry import RetryPolicy

log = logging.getLogger("musubi.sdk")


class AsyncMusubiClient:
    """Async HTTP client over the canonical Musubi API."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        retry: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | httpx.MockTransport | None = None,
        strict_version: bool = False,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._retry = retry or RetryPolicy.default()
        self._strict_version = strict_version
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
            headers={_BEARER_HEADER: f"Bearer {token}"},
        )
        self.episodic = _AsyncEpisodic(self)
        self.curated = _AsyncCurated(self)
        self.concepts = _AsyncConcepts(self)
        self.artifacts = _AsyncArtifacts(self)
        self.thoughts = _AsyncThoughts(self)
        self.lifecycle = _AsyncLifecycle(self)
        self.ops = _AsyncOps(self)

    # ------------------------------------------------------------------
    # Top-level methods
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        *,
        namespace: str,
        query_text: str,
        mode: str = "fast",
        limit: int = 10,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            "/retrieve",
            operation_name="retrieve",
            json_body={
                "namespace": namespace,
                "query_text": query_text,
                "mode": mode,
                "limit": limit,
            },
            request_id=request_id,
        )

    async def retrieve_stream(
        self,
        *,
        namespace: str,
        query_text: str,
        mode: str = "fast",
        limit: int = 10,
        request_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        body = {
            "namespace": namespace,
            "query_text": query_text,
            "mode": mode,
            "limit": limit,
        }
        headers = self._headers(request_id=request_id, idempotency_key=None, post=False)
        full_url = str(self._http.base_url.join("/retrieve/stream"))
        from musubi.sdk.tracing import sdk_span

        with sdk_span(
            "retrieve_stream", "POST", full_url, namespace=namespace, request_id=request_id
        ):
            async with self._http.stream(
                "POST", "/retrieve/stream", json=body, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise self._exception_from_response(resp)
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    yield json.loads(line)

    async def probe_version(self) -> str:
        body = await self._json("GET", "/ops/status", operation_name="probe_version")
        observed = str(body.get("version", "0.0.0"))
        if _is_older(observed, _MIN_CORE_VERSION):
            msg = (
                f"Musubi Core version {observed!r} is older than SDK minimum "
                f"{_MIN_CORE_VERSION!r}; expect missing endpoints"
            )
            if self._strict_version:
                raise MusubiError(code="INTERNAL", detail=msg)
            log.warning(msg)
        return observed

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> AsyncMusubiClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Internal — request shape
    # ------------------------------------------------------------------

    def _headers(
        self,
        *,
        request_id: str | None,
        idempotency_key: str | None,
        post: bool,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        if request_id is not None:
            headers[_REQUEST_ID_HEADER] = request_id
        if post:
            headers[_IDEMPOTENCY_HEADER] = idempotency_key or uuid.uuid4().hex
        elif idempotency_key is not None:
            headers[_IDEMPOTENCY_HEADER] = idempotency_key
        return headers

    async def _json(
        self,
        method: str,
        path: str,
        *,
        operation_name: str = "unknown",
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        resp = await self._request(
            method,
            path,
            operation_name=operation_name,
            json_body=json_body,
            params=params,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        if resp.status_code == 204:
            return {}
        return resp.json()  # type: ignore[no-any-return]

    async def _bytes(
        self,
        method: str,
        path: str,
        *,
        operation_name: str = "unknown",
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> bytes:
        resp = await self._request(
            method, path, operation_name=operation_name, params=params, request_id=request_id
        )
        return resp.content

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation_name: str = "unknown",
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        is_post = method.upper() == "POST"
        headers = self._headers(
            request_id=request_id,
            idempotency_key=idempotency_key,
            post=is_post,
        )
        ns = None
        if json_body and "namespace" in json_body:
            ns = json_body["namespace"]
        elif params and "namespace" in params:
            ns = params["namespace"]
        full_url = str(self._http.base_url.join(path))
        from musubi.sdk.tracing import sdk_span

        with sdk_span(operation_name, method, full_url, namespace=ns, request_id=request_id):
            ns = None
            if json_body and "namespace" in json_body:
                ns = json_body["namespace"]
            elif params and "namespace" in params:
                ns = params["namespace"]
            full_url = str(self._http.base_url.join(path))
            from musubi.sdk.tracing import sdk_span

            with sdk_span(operation_name, method, full_url, namespace=ns, request_id=request_id):
                last_retry_after: float | None = None
                for attempt in range(1, self._retry.max_attempts + 1):
                    if attempt > 1:
                        delay = self._retry.backoff_for(attempt, retry_after=last_retry_after)
                        if delay > 0:
                            await asyncio.sleep(delay)
                    try:
                        resp = await self._http.request(
                            method,
                            path,
                            json=json_body,
                            params=params,
                            headers=headers,
                        )
                    except httpx.HTTPError as exc:
                        if attempt >= self._retry.max_attempts:
                            raise NetworkError(
                                code="NETWORK_ERROR",
                                detail=f"transport error: {exc!r}",
                            ) from exc
                        continue
                    if resp.status_code in self._retry.retryable_statuses:
                        last_retry_after = _retry_after_from(resp.headers)
                        if attempt >= self._retry.max_attempts:
                            raise self._exception_from_response(resp)
                        continue
                    if resp.status_code >= 400:
                        raise self._exception_from_response(resp)
                    return resp
                raise RuntimeError("unreachable: retry loop exited without return")

    @staticmethod
    def _exception_from_response(resp: httpx.Response) -> MusubiError:
        try:
            body = resp.json()
            err = body.get("error", {}) if isinstance(body, dict) else {}
            code = str(err.get("code", "INTERNAL"))
            detail = str(err.get("detail", resp.text or "unknown error"))
            hint = str(err.get("hint", ""))
        except (ValueError, AttributeError):
            code = "INTERNAL"
            detail = resp.text or "unknown error"
            hint = ""
        cls = exception_for_status(resp.status_code)
        return cls(code=code, detail=detail, hint=hint, status_code=resp.status_code)


# ---------------------------------------------------------------------------
# Resource namespaces — async
# ---------------------------------------------------------------------------


class _AsyncEpisodic:
    def __init__(self, client: AsyncMusubiClient) -> None:
        self._c = client

    async def capture(
        self,
        *,
        namespace: str,
        content: str,
        tags: list[str] | None = None,
        topics: list[str] | None = None,
        importance: int = 5,
        idempotency_key: str | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Capture an episodic memory (async).

        ``created_at`` is a migration / replay escape hatch: pass a
        ``datetime`` to preserve a source-truth timestamp on the row.
        The server requires the bearer token to carry ``operator``
        scope when this field is present; a non-operator token 403s.
        Omit for normal captures.
        """
        body: dict[str, Any] = {
            "namespace": namespace,
            "content": content,
            "tags": tags or [],
            "topics": topics or [],
            "importance": importance,
        }
        if created_at is not None:
            body["created_at"] = _ensure_tz_aware_created_at(created_at)
        return await self._c._json(
            "POST",
            "/episodic",
            operation_name="episodic.capture",
            json_body=body,
            idempotency_key=idempotency_key,
        )

    async def capture_result(self, **kw: Any) -> SDKResult[dict[str, Any]]:
        try:
            return SDKResult(ok=await self.capture(**kw))
        except MusubiError as exc:
            return SDKResult(err=exc)

    async def get(self, *, namespace: str, object_id: str) -> dict[str, Any]:
        return await self._c._json(
            "GET",
            f"/episodic/{object_id}",
            operation_name="episodic.get",
            params={"namespace": namespace},
        )

    def batch(self, *, namespace: str) -> _AsyncBatchContext:
        return _AsyncBatchContext(client=self._c, namespace=namespace)


class _AsyncBatchContext:
    """Async context manager for batch capture; one POST on exit."""

    def __init__(self, *, client: AsyncMusubiClient, namespace: str) -> None:
        self._c = client
        self._namespace = namespace
        self._items: list[dict[str, Any]] = []
        self.results: dict[str, Any] | None = None

    def capture(
        self,
        *,
        content: str,
        tags: list[str] | None = None,
        importance: int = 5,
        created_at: datetime | None = None,
    ) -> None:
        """Queue one row into the async batch. ``created_at`` is the
        migration override; the whole batch is 403'd on flush unless the
        bearer carries ``operator`` scope when any item sets it."""
        item: dict[str, Any] = {
            "content": content,
            "tags": tags or [],
            "importance": importance,
        }
        if created_at is not None:
            item["created_at"] = _ensure_tz_aware_created_at(created_at)
        self._items.append(item)

    async def __aenter__(self) -> _AsyncBatchContext:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if not self._items:
            return
        self.results = await self._c._json(
            "POST",
            "/episodic/batch",
            operation_name="episodic.batch.capture",
            json_body={"namespace": self._namespace, "items": self._items},
        )


class _AsyncCurated:
    def __init__(self, client: AsyncMusubiClient) -> None:
        self._c = client

    async def get(self, *, namespace: str, object_id: str) -> dict[str, Any]:
        return await self._c._json(
            "GET",
            f"/curated/{object_id}",
            operation_name="curated.get",
            params={"namespace": namespace},
        )


class _AsyncConcepts:
    def __init__(self, client: AsyncMusubiClient) -> None:
        self._c = client

    async def get(self, *, namespace: str, object_id: str) -> dict[str, Any]:
        return await self._c._json(
            "GET",
            f"/concepts/{object_id}",
            operation_name="concepts.get",
            params={"namespace": namespace},
        )


class _AsyncArtifacts:
    def __init__(self, client: AsyncMusubiClient) -> None:
        self._c = client

    async def get(self, *, namespace: str, object_id: str) -> dict[str, Any]:
        return await self._c._json(
            "GET",
            f"/artifacts/{object_id}",
            operation_name="artifacts.get",
            params={"namespace": namespace},
        )

    async def blob(self, *, namespace: str, object_id: str) -> bytes:
        return await self._c._bytes(
            "GET",
            f"/artifacts/{object_id}/blob",
            operation_name="artifacts.blob",
            params={"namespace": namespace},
        )


class _AsyncThoughts:
    def __init__(self, client: AsyncMusubiClient) -> None:
        self._c = client

    async def send(
        self,
        *,
        namespace: str,
        from_presence: str,
        to_presence: str,
        content: str,
        channel: str = "default",
        importance: int = 5,
    ) -> dict[str, Any]:
        return await self._c._json(
            "POST",
            "/thoughts/send",
            operation_name="thoughts.send",
            json_body={
                "namespace": namespace,
                "from_presence": from_presence,
                "to_presence": to_presence,
                "content": content,
                "channel": channel,
                "importance": importance,
            },
        )

    async def check(self, *, namespace: str, presence: str) -> dict[str, Any]:
        return await self._c._json(
            "POST",
            "/thoughts/check",
            operation_name="thoughts.check",
            json_body={"namespace": namespace, "presence": presence},
        )


class _AsyncLifecycle:
    def __init__(self, client: AsyncMusubiClient) -> None:
        self._c = client

    async def events(self, *, namespace: str | None = None) -> dict[str, Any]:
        return await self._c._json(
            "GET",
            "/lifecycle/events",
            operation_name="lifecycle.events",
            params={"namespace": namespace} if namespace else None,
        )


class _AsyncOps:
    def __init__(self, client: AsyncMusubiClient) -> None:
        self._c = client

    async def health(self) -> dict[str, Any]:
        return await self._c._json("GET", "/ops/health", operation_name="ops.health")

    async def status(self) -> dict[str, Any]:
        return await self._c._json("GET", "/ops/status", operation_name="ops.status")


__all__ = ["AsyncMusubiClient"]
