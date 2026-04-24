"""Sync :class:`MusubiClient` wrapping ``httpx.Client``.

Resource namespaces (`episodic`, `curated`, `concepts`, `artifacts`,
`thoughts`, `lifecycle`, `ops`) hang off the client instance per the
spec; the top-level ``retrieve`` / ``retrieve_stream`` / ``probe_version``
methods live directly on the client.

Every HTTP call goes through :meth:`_request` which:
- builds the URL relative to ``base_url``,
- attaches the bearer + correlation headers,
- auto-mints an Idempotency-Key on POST when not supplied,
- runs the retry policy,
- maps non-2xx responses to typed exceptions via
  :func:`musubi.sdk.exceptions.exception_for_status`.

The async variant in ``async_client.py`` mirrors this surface
function-for-function over ``httpx.AsyncClient``.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import httpx

from musubi.sdk.exceptions import (
    MusubiError,
    NetworkError,
    exception_for_status,
)
from musubi.sdk.result import SDKResult
from musubi.sdk.retry import RetryPolicy

log = logging.getLogger("musubi.sdk")


def _ensure_tz_aware_created_at(value: datetime) -> str:
    """Normalise a caller-supplied ``created_at`` for the wire.

    The server rejects naive datetimes (``tzinfo is None``) because
    the storage layer requires UTC. Failing fast in the SDK prevents
    an avoidable round-trip and gives a clearer error than the 4xx
    the server would return. The returned string is ISO-8601 with
    offset, suitable for direct JSON serialisation."""
    if value.tzinfo is None:
        raise ValueError(
            "created_at must be timezone-aware; pass e.g. "
            "datetime.now(tz=timezone.utc), not datetime.utcnow()"
        )
    return value.isoformat()


_MIN_CORE_VERSION = "0.1.0"
"""SDK's minimum supported Core version. Probe-time check warns or
raises if the live Core advertises a lower version."""

_BEARER_HEADER = "Authorization"
_REQUEST_ID_HEADER = "X-Request-Id"
_IDEMPOTENCY_HEADER = "Idempotency-Key"


def _is_older(observed: str, minimum: str) -> bool:
    """Tuple-of-ints version comparison (major.minor.patch)."""

    def _parse(v: str) -> tuple[int, ...]:
        return tuple(int(p) for p in v.split(".") if p.isdigit())

    return _parse(observed) < _parse(minimum)


def _retry_after_from(headers: httpx.Headers) -> float | None:
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class MusubiClient:
    """Sync HTTP client over the canonical Musubi API."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        retry: RetryPolicy | None = None,
        transport: httpx.BaseTransport | None = None,
        strict_version: bool = False,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._retry = retry or RetryPolicy.default()
        self._strict_version = strict_version
        self._http = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
            headers={_BEARER_HEADER: f"Bearer {token}"},
        )
        # Resource namespaces.
        self.episodic = _Episodic(self)
        self.curated = _Curated(self)
        self.concepts = _Concepts(self)
        self.artifacts = _Artifacts(self)
        self.thoughts = _Thoughts(self)
        self.lifecycle = _Lifecycle(self)
        self.ops = _Ops(self)

    # ------------------------------------------------------------------
    # Top-level methods
    # ------------------------------------------------------------------

    def retrieve(
        self,
        *,
        namespace: str,
        query_text: str,
        mode: str = "fast",
        limit: int = 10,
        planes: list[str] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "namespace": namespace,
            "query_text": query_text,
            "mode": mode,
            "limit": limit,
        }
        if planes is not None:
            body["planes"] = planes
        return self._json(
            "POST",
            "/retrieve",
            operation_name="retrieve",
            json_body=body,
            request_id=request_id,
        )

    def retrieve_stream(
        self,
        *,
        namespace: str,
        query_text: str,
        mode: str = "fast",
        limit: int = 10,
        request_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        body = {
            "namespace": namespace,
            "query_text": query_text,
            "mode": mode,
            "limit": limit,
        }
        headers = self._headers(request_id=request_id, idempotency_key=None, post=False)
        # Streaming bypasses the retry loop — generator semantics make
        # mid-stream retry fragile. Caller can retry by re-iterating.
        with self._http.stream("POST", "/retrieve/stream", json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise self._exception_from_response(resp)
            for line in resp.iter_lines():
                if not line:
                    continue
                yield json.loads(line)

    def probe_version(self) -> str:
        """Probe the live Core's reported version. Logs a warning (or
        raises in strict mode) if it's older than the SDK's minimum."""
        body = self._json("GET", "/ops/status", operation_name="probe_version")
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

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> MusubiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

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

    def _json(
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
        resp = self._request(
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

    def _bytes(
        self,
        method: str,
        path: str,
        *,
        operation_name: str = "unknown",
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> bytes:
        resp = self._request(
            method, path, operation_name=operation_name, params=params, request_id=request_id
        )
        return resp.content

    def _request(
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
            last_exc: Exception | None = None
            for attempt in range(1, self._retry.max_attempts + 1):
                if attempt > 1:
                    # Honour Retry-After from the previous response if the
                    # last attempt was retry-eligible.
                    ra: float | None = None
                    if isinstance(last_exc, _RetryableHTTP):
                        ra = last_exc.retry_after
                    delay = self._retry.backoff_for(attempt, retry_after=ra)
                    if delay > 0:
                        time.sleep(delay)
                try:
                    resp = self._http.request(
                        method,
                        path,
                        json=json_body,
                        params=params,
                        headers=headers,
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt >= self._retry.max_attempts:
                        raise NetworkError(
                            code="NETWORK_ERROR",
                            detail=f"transport error: {exc!r}",
                        ) from exc
                    continue
                if resp.status_code in self._retry.retryable_statuses:
                    last_exc = _RetryableHTTP(resp)
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


class _RetryableHTTP(Exception):
    """Carries a retry-eligible response between iterations of the
    retry loop. Not part of the public API."""

    def __init__(self, resp: httpx.Response) -> None:
        super().__init__(f"retryable status {resp.status_code}")
        self.resp = resp
        self.retry_after = _retry_after_from(resp.headers)


# ---------------------------------------------------------------------------
# Resource namespaces — sync
# ---------------------------------------------------------------------------


class _Episodic:
    def __init__(self, client: MusubiClient) -> None:
        self._c = client

    def capture(
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
        """Capture an episodic memory.

        ``created_at`` is a migration / replay escape hatch: pass a
        ``datetime`` to preserve a source-truth timestamp on the row.
        The server requires the bearer token to carry ``operator`` scope
        when this field is present; a non-operator token 403s. Omit
        for normal captures — Musubi stamps created_at at ingest time.
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
        return self._c._json(
            "POST",
            "/episodic",
            operation_name="episodic.capture",
            json_body=body,
            idempotency_key=idempotency_key,
        )

    def capture_result(self, **kw: Any) -> SDKResult[dict[str, Any]]:
        try:
            return SDKResult(ok=self.capture(**kw))
        except MusubiError as exc:
            return SDKResult(err=exc)

    def get(self, *, namespace: str, object_id: str) -> dict[str, Any]:
        return self._c._json(
            "GET",
            f"/episodic/{object_id}",
            operation_name="episodic.get",
            params={"namespace": namespace},
        )

    def batch(self, *, namespace: str) -> _BatchContext:
        return _BatchContext(client=self._c, namespace=namespace)


class _BatchContext:
    """Context manager for batch capture; flushes one POST on exit."""

    def __init__(self, *, client: MusubiClient, namespace: str) -> None:
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
        """Queue one row into the batch. ``created_at`` is the migration
        override; the whole batch is 403'd on flush unless the bearer
        carries ``operator`` scope when any item sets it."""
        item: dict[str, Any] = {
            "content": content,
            "tags": tags or [],
            "importance": importance,
        }
        if created_at is not None:
            item["created_at"] = _ensure_tz_aware_created_at(created_at)
        self._items.append(item)

    def __enter__(self) -> _BatchContext:
        return self

    def __exit__(self, *exc_info: object) -> None:
        if not self._items:
            return
        self.results = self._c._json(
            "POST",
            "/episodic/batch",
            operation_name="episodic.batch.capture",
            json_body={"namespace": self._namespace, "items": self._items},
        )


class _Curated:
    def __init__(self, client: MusubiClient) -> None:
        self._c = client

    def get(self, *, namespace: str, object_id: str) -> dict[str, Any]:
        return self._c._json(
            "GET",
            f"/curated/{object_id}",
            operation_name="curated.get",
            params={"namespace": namespace},
        )


class _Concepts:
    def __init__(self, client: MusubiClient) -> None:
        self._c = client

    def get(self, *, namespace: str, object_id: str) -> dict[str, Any]:
        return self._c._json(
            "GET",
            f"/concepts/{object_id}",
            operation_name="concepts.get",
            params={"namespace": namespace},
        )


class _Artifacts:
    def __init__(self, client: MusubiClient) -> None:
        self._c = client

    def get(self, *, namespace: str, object_id: str) -> dict[str, Any]:
        return self._c._json(
            "GET",
            f"/artifacts/{object_id}",
            operation_name="artifacts.get",
            params={"namespace": namespace},
        )

    def blob(self, *, namespace: str, object_id: str) -> bytes:
        return self._c._bytes(
            "GET",
            f"/artifacts/{object_id}/blob",
            operation_name="artifacts.blob",
            params={"namespace": namespace},
        )


class _Thoughts:
    def __init__(self, client: MusubiClient) -> None:
        self._c = client

    def send(
        self,
        *,
        namespace: str,
        from_presence: str,
        to_presence: str,
        content: str,
        channel: str = "default",
        importance: int = 5,
    ) -> dict[str, Any]:
        return self._c._json(
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

    def check(self, *, namespace: str, presence: str) -> dict[str, Any]:
        return self._c._json(
            "POST",
            "/thoughts/check",
            operation_name="thoughts.check",
            json_body={"namespace": namespace, "presence": presence},
        )


class _Lifecycle:
    def __init__(self, client: MusubiClient) -> None:
        self._c = client

    def events(self, *, namespace: str | None = None) -> dict[str, Any]:
        return self._c._json(
            "GET",
            "/lifecycle/events",
            operation_name="lifecycle.events",
            params={"namespace": namespace} if namespace else None,
        )


class _Ops:
    def __init__(self, client: MusubiClient) -> None:
        self._c = client

    def health(self) -> dict[str, Any]:
        return self._c._json("GET", "/ops/health", operation_name="ops.health")

    def status(self) -> dict[str, Any]:
        return self._c._json("GET", "/ops/status", operation_name="ops.status")


__all__ = ["MusubiClient"]
