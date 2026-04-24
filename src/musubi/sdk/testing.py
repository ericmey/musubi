"""Test fixtures for SDK consumers.

:class:`FakeMusubiClient` accepts the same constructor signature as
:class:`musubi.sdk.MusubiClient` so adapters can swap one for the other
in unit tests without changing call sites. Per-method canned-return
kwargs (``capture_returns``, ``retrieve_returns``, etc.) shape what each
method returns; unconfigured methods raise ``NotImplementedError`` so a
test that exercises an un-faked path fails loudly instead of silently
returning an empty dict.

Per [[07-interfaces/sdk]] § Test contract bullets 20-21.
"""

from __future__ import annotations

from typing import Any

from musubi.sdk.exceptions import MusubiError
from musubi.sdk.result import SDKResult
from musubi.sdk.retry import RetryPolicy

_UNSET: dict[str, Any] = {"__unset__": True}


def _is_unset(value: dict[str, Any]) -> bool:
    return value is _UNSET


class FakeMusubiClient:
    """Drop-in fake mirroring :class:`MusubiClient`'s public surface."""

    def __init__(
        self,
        *,
        base_url: str = "https://fake.musubi.test/v1",
        token: str = "fake-token",
        timeout: float = 30.0,
        retry: RetryPolicy | None = None,
        transport: object | None = None,
        strict_version: bool = False,
        # Canned per-method returns. Unset → method raises.
        capture_returns: dict[str, Any] | None = None,
        capture_error: MusubiError | None = None,
        get_memory_returns: dict[str, Any] | None = None,
        retrieve_returns: dict[str, Any] | None = None,
        retrieve_stream_returns: list[dict[str, Any]] | None = None,
        thoughts_send_returns: dict[str, Any] | None = None,
        thoughts_check_returns: dict[str, Any] | None = None,
        curated_get_returns: dict[str, Any] | None = None,
        concept_get_returns: dict[str, Any] | None = None,
        artifact_get_returns: dict[str, Any] | None = None,
        artifact_blob_returns: bytes | None = None,
        lifecycle_events_returns: dict[str, Any] | None = None,
        ops_health_returns: dict[str, Any] | None = None,
        ops_status_returns: dict[str, Any] | None = None,
        probe_version_returns: str = "0.1.0",
    ) -> None:
        # Accept-and-ignore the real-client kwargs so adapters can swap.
        self._base_url = base_url
        self._token = token
        self._timeout = timeout
        self._retry = retry or RetryPolicy.default()
        self._transport = transport
        self._strict_version = strict_version

        self._capture_returns = capture_returns
        self._capture_error = capture_error
        self._get_memory_returns = get_memory_returns
        self._retrieve_returns = retrieve_returns
        self._retrieve_stream_returns = retrieve_stream_returns or []
        self._thoughts_send_returns = thoughts_send_returns
        self._thoughts_check_returns = thoughts_check_returns
        self._curated_get_returns = curated_get_returns
        self._concept_get_returns = concept_get_returns
        self._artifact_get_returns = artifact_get_returns
        self._artifact_blob_returns = artifact_blob_returns
        self._lifecycle_events_returns = lifecycle_events_returns
        self._ops_health_returns = ops_health_returns
        self._ops_status_returns = ops_status_returns
        self._probe_version_returns = probe_version_returns

        # Call log so adapter tests can assert on call shape.
        self.calls: list[tuple[str, dict[str, Any]]] = []

        # Resource namespaces.
        self.episodic = _FakeEpisodic(self)
        self.curated = _FakeCurated(self)
        self.concepts = _FakeConcepts(self)
        self.artifacts = _FakeArtifacts(self)
        self.thoughts = _FakeThoughts(self)
        self.lifecycle = _FakeLifecycle(self)
        self.ops = _FakeOps(self)

    # ------------------------------------------------------------------
    # Top-level
    # ------------------------------------------------------------------

    def retrieve(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("retrieve", kw))
        if self._retrieve_returns is None:
            raise NotImplementedError("FakeMusubiClient: retrieve_returns not configured")
        return self._retrieve_returns

    def retrieve_stream(self, **kw: Any) -> Any:
        self.calls.append(("retrieve_stream", kw))
        return iter(self._retrieve_stream_returns)

    def probe_version(self) -> str:
        self.calls.append(("probe_version", {}))
        return self._probe_version_returns

    def close(self) -> None:
        pass

    def __enter__(self) -> FakeMusubiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class _FakeEpisodic:
    def __init__(self, client: FakeMusubiClient) -> None:
        self._c = client

    def capture(self, **kw: Any) -> dict[str, Any]:
        self._c.calls.append(("episodic.capture", kw))
        if self._c._capture_error is not None:
            raise self._c._capture_error
        if self._c._capture_returns is None:
            raise NotImplementedError("FakeMusubiClient: capture_returns not configured")
        return self._c._capture_returns

    def capture_result(self, **kw: Any) -> SDKResult[dict[str, Any]]:
        try:
            return SDKResult(ok=self.capture(**kw))
        except MusubiError as exc:
            return SDKResult(err=exc)

    def get(self, **kw: Any) -> dict[str, Any]:
        self._c.calls.append(("episodic.get", kw))
        if self._c._get_memory_returns is None:
            raise NotImplementedError("FakeMusubiClient: get_memory_returns not configured")
        return self._c._get_memory_returns

    def batch(self, **kw: Any) -> _FakeBatchContext:
        return _FakeBatchContext(self._c)


class _FakeBatchContext:
    def __init__(self, client: FakeMusubiClient) -> None:
        self._c = client
        self.results: dict[str, Any] | None = None

    def capture(self, **kw: Any) -> None:
        self._c.calls.append(("episodic.batch.capture", kw))

    def __enter__(self) -> _FakeBatchContext:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class _FakeCurated:
    def __init__(self, client: FakeMusubiClient) -> None:
        self._c = client

    def get(self, **kw: Any) -> dict[str, Any]:
        self._c.calls.append(("curated.get", kw))
        if self._c._curated_get_returns is None:
            raise NotImplementedError("FakeMusubiClient: curated_get_returns not configured")
        return self._c._curated_get_returns


class _FakeConcepts:
    def __init__(self, client: FakeMusubiClient) -> None:
        self._c = client

    def get(self, **kw: Any) -> dict[str, Any]:
        self._c.calls.append(("concepts.get", kw))
        if self._c._concept_get_returns is None:
            raise NotImplementedError("FakeMusubiClient: concept_get_returns not configured")
        return self._c._concept_get_returns


class _FakeArtifacts:
    def __init__(self, client: FakeMusubiClient) -> None:
        self._c = client

    def get(self, **kw: Any) -> dict[str, Any]:
        self._c.calls.append(("artifacts.get", kw))
        if self._c._artifact_get_returns is None:
            raise NotImplementedError("FakeMusubiClient: artifact_get_returns not configured")
        return self._c._artifact_get_returns

    def blob(self, **kw: Any) -> bytes:
        self._c.calls.append(("artifacts.blob", kw))
        if self._c._artifact_blob_returns is None:
            raise NotImplementedError("FakeMusubiClient: artifact_blob_returns not configured")
        return self._c._artifact_blob_returns


class _FakeThoughts:
    def __init__(self, client: FakeMusubiClient) -> None:
        self._c = client

    def send(self, **kw: Any) -> dict[str, Any]:
        self._c.calls.append(("thoughts.send", kw))
        if self._c._thoughts_send_returns is None:
            raise NotImplementedError("FakeMusubiClient: thoughts_send_returns not configured")
        return self._c._thoughts_send_returns

    def check(self, **kw: Any) -> dict[str, Any]:
        self._c.calls.append(("thoughts.check", kw))
        if self._c._thoughts_check_returns is None:
            raise NotImplementedError("FakeMusubiClient: thoughts_check_returns not configured")
        return self._c._thoughts_check_returns


class _FakeLifecycle:
    def __init__(self, client: FakeMusubiClient) -> None:
        self._c = client

    def events(self, **kw: Any) -> dict[str, Any]:
        self._c.calls.append(("lifecycle.events", kw))
        if self._c._lifecycle_events_returns is None:
            raise NotImplementedError("FakeMusubiClient: lifecycle_events_returns not configured")
        return self._c._lifecycle_events_returns


class _FakeOps:
    def __init__(self, client: FakeMusubiClient) -> None:
        self._c = client

    def health(self) -> dict[str, Any]:
        self._c.calls.append(("ops.health", {}))
        if self._c._ops_health_returns is None:
            raise NotImplementedError("FakeMusubiClient: ops_health_returns not configured")
        return self._c._ops_health_returns

    def status(self) -> dict[str, Any]:
        self._c.calls.append(("ops.status", {}))
        if self._c._ops_status_returns is None:
            raise NotImplementedError("FakeMusubiClient: ops_status_returns not configured")
        return self._c._ops_status_returns


class _AsyncFakeBatchContext:
    def __init__(self, sync_batch: Any) -> None:
        self._sync = sync_batch
        self.results: dict[str, Any] | None = None

    def capture(self, **kw: Any) -> None:
        self._sync.capture(**kw)

    async def __aenter__(self) -> _AsyncFakeBatchContext:
        self._sync.__enter__()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._sync.__exit__(*exc_info)
        self.results = self._sync.results


class _AsyncEpisodicFake:
    def __init__(self, sync_ns: Any) -> None:
        self._ns = sync_ns

    async def capture(self, **kw: Any) -> dict[str, Any]:
        return self._ns.capture(**kw)  # type: ignore

    async def capture_result(self, **kw: Any) -> SDKResult[dict[str, Any]]:
        return self._ns.capture_result(**kw)  # type: ignore

    async def get(self, **kw: Any) -> dict[str, Any]:
        return self._ns.get(**kw)  # type: ignore

    def batch(self, **kw: Any) -> _AsyncFakeBatchContext:
        sync_batch = self._ns.batch(**kw)
        return _AsyncFakeBatchContext(sync_batch)


class _AsyncCuratedFake:
    def __init__(self, sync_ns: Any) -> None:
        self._ns = sync_ns

    async def get(self, **kw: Any) -> dict[str, Any]:
        return self._ns.get(**kw)  # type: ignore


class _AsyncConceptsFake:
    def __init__(self, sync_ns: Any) -> None:
        self._ns = sync_ns

    async def get(self, **kw: Any) -> dict[str, Any]:
        return self._ns.get(**kw)  # type: ignore


class _AsyncArtifactsFake:
    def __init__(self, sync_ns: Any) -> None:
        self._ns = sync_ns

    async def get(self, **kw: Any) -> dict[str, Any]:
        return self._ns.get(**kw)  # type: ignore

    async def blob(self, **kw: Any) -> bytes:
        return self._ns.blob(**kw)  # type: ignore


class _AsyncThoughtsFake:
    def __init__(self, sync_ns: Any) -> None:
        self._ns = sync_ns

    async def send(self, **kw: Any) -> dict[str, Any]:
        return self._ns.send(**kw)  # type: ignore

    async def check(self, **kw: Any) -> dict[str, Any]:
        return self._ns.check(**kw)  # type: ignore


class _AsyncLifecycleFake:
    def __init__(self, sync_ns: Any) -> None:
        self._ns = sync_ns

    async def events(self, **kw: Any) -> dict[str, Any]:
        return self._ns.events(**kw)  # type: ignore


class _AsyncOpsFake:
    def __init__(self, sync_ns: Any) -> None:
        self._ns = sync_ns

    async def health(self, **kw: Any) -> dict[str, Any]:
        return self._ns.health(**kw)  # type: ignore

    async def status(self, **kw: Any) -> dict[str, Any]:
        return self._ns.status(**kw)  # type: ignore


class AsyncFakeMusubiClient:
    """Async drop-in fake mirroring AsyncMusubiClient's public surface.

    Same constructor signature + canned-return kwargs as
    FakeMusubiClient; every method that's async on AsyncMusubiClient
    is async here too. Shares the calls log shape so adapter tests
    can assert against (method_name, kwargs) tuples identically."""

    def __init__(self, **kwargs: Any) -> None:
        self._fake = FakeMusubiClient(**kwargs)
        self.calls = self._fake.calls

        self.episodic = _AsyncEpisodicFake(self._fake.episodic)
        self.curated = _AsyncCuratedFake(self._fake.curated)
        self.concepts = _AsyncConceptsFake(self._fake.concepts)
        self.artifacts = _AsyncArtifactsFake(self._fake.artifacts)
        self.thoughts = _AsyncThoughtsFake(self._fake.thoughts)
        self.lifecycle = _AsyncLifecycleFake(self._fake.lifecycle)
        self.ops = _AsyncOpsFake(self._fake.ops)
        self._upload_handler: Any = None

    async def retrieve(self, **kw: Any) -> dict[str, Any]:
        return self._fake.retrieve(**kw)

    async def retrieve_stream(self, **kw: Any) -> Any:
        sync_stream = self._fake.retrieve_stream(**kw)
        for item in sync_stream:
            yield item

    async def probe_version(self) -> str:
        return self._fake.probe_version()

    async def close(self) -> None:
        self._fake.close()

    async def __aenter__(self) -> AsyncFakeMusubiClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


__all__ = ["AsyncFakeMusubiClient", "FakeMusubiClient"]
