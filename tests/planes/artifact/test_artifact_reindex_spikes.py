"""ART-001 real-Qdrant design spike acceptance tests.

These tests are tests-only. They do NOT modify `src/`. They run
against an ephemeral local Docker Qdrant (NOT `:memory:`) bound
to 127.0.0.1 on a collision-free port. The container is started
in a module-scoped fixture and removed on exit. Each test records
the observed result for the spike report.

Per Yua 00:24:28 + 00:28:44: this is a design-spike, not an
implementation. The tests demonstrate the REV3 acceptance
invariants against a real persistent Qdrant; they do NOT close
ART-001.

The existing `tests/planes/test_artifact.py` is NOT modified unless
strictly necessary. The spike acceptance file is separate.

Uses only the standard library (`urllib`) and existing
dependencies (`qdrant-client`, `requests` is intentionally
avoided; the spike uses `urllib` so the spike is reproducible
without adding new runtime dependencies to the Musubi repo).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import platform
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from typing import Any, Protocol, cast
from unittest.mock import Mock

import pytest
from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.planes.artifact import ArtifactPlane
from musubi.store.specs import DENSE_SIZE, DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.artifact import SourceArtifact
from musubi.types.common import epoch_of, generate_ksuid, utc_now

# Independently resolved from the OCI image index with both
# `docker buildx imagetools inspect --raw` and `docker manifest inspect`.
DIGEST_AMD64 = "cd3e42737c684ee516ae5533218be93fd5288f41d0a466ed18dbdc22ef52a000"
DIGEST_ARM64 = "3fd57e61606ed61c48c91c4131cba6808f01b0879f5478fd011573189855bba1"
QDRANT_VERSION = "1.17.1"
_PLATFORM_BY_MACHINE = {
    "aarch64": ("linux/arm64", DIGEST_ARM64),
    "arm64": ("linux/arm64", DIGEST_ARM64),
    "amd64": ("linux/amd64", DIGEST_AMD64),
    "x86_64": ("linux/amd64", DIGEST_AMD64),
}


def _qdrant_image_for_host() -> tuple[str, str]:
    machine = platform.machine().lower()
    try:
        docker_platform, digest = _PLATFORM_BY_MACHINE[machine]
    except KeyError as exc:
        raise RuntimeError(
            f"unsupported Docker host architecture for ART-001 spike: {machine}"
        ) from exc
    return docker_platform, f"qdrant/qdrant@sha256:{digest}"


def _find_free_port() -> int:
    """Find a free TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _http_get(url: str, timeout_s: float = 2.0) -> tuple[int, str, str]:
    """GET a URL via urllib. Returns (status, body, server_header).
    Standard library only; no new runtime dependencies."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return (r.status, r.read().decode("utf-8", errors="replace"), r.headers.get("server", ""))


def _wait_for_qdrant_health(base_url: str, deadline_s: float) -> bool:
    """Bounded wait for /readyz plus server root probe (per
    Yua 00:28:44: probe via /readyz plus server version endpoint,
    not --version/--help)."""
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            status, _, _ = _http_get(f"{base_url}/readyz")
            if status in (200, 204):
                # Also probe the server root for the spike record.
                with contextlib.suppress(urllib.error.URLError, urllib.error.HTTPError, OSError):
                    _http_get(f"{base_url}/")
                return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            pass
        time.sleep(0.5)
    return False


@pytest.fixture(scope="module")
def qdrant_server() -> Iterator[tuple[str, str]]:
    """Start an ephemeral local Docker Qdrant pinned to v1.17.1
    using the verified digest for the Docker host architecture,
    bound to 127.0.0.1 on a
    collision-free port with a temporary volume/network that is
    removed on exit.

    The container runs the Qdrant server. We do NOT modify the
    source code. The container exposes its HTTP port and gRPC
    port mapped to 127.0.0.1 only (NOT all interfaces).
    """
    port_http = _find_free_port()
    port_grpc = _find_free_port()
    while port_grpc == port_http:
        port_grpc = _find_free_port()
    container_name = f"art001-spike-{secrets.token_hex(4)}"
    network_name = f"art001-net-{secrets.token_hex(4)}"
    volume_name = f"art001-vol-{secrets.token_hex(4)}"
    docker_platform, image = _qdrant_image_for_host()

    # Create a dedicated user-defined bridge network for the spike.
    subprocess.run(
        ["docker", "network", "create", "--driver", "bridge", network_name],
        check=True,
        capture_output=True,
        text=True,
    )
    # Create a dedicated volume for the spike.
    subprocess.run(
        ["docker", "volume", "create", volume_name],
        check=True,
        capture_output=True,
        text=True,
    )
    container_id: str | None = None
    try:
        # Run the container bound to 127.0.0.1 only on a collision-free
        # port; do NOT expose to all interfaces.
        proc = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--platform",
                docker_platform,
                "--name",
                container_name,
                "--network",
                network_name,
                "-v",
                f"{volume_name}:/qdrant/storage",
                "-p",
                f"127.0.0.1:{port_http}:6333",
                "-p",
                f"127.0.0.1:{port_grpc}:6334",
                image,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        container_id = proc.stdout.strip()
        base_url = f"http://127.0.0.1:{port_http}"
        if not _wait_for_qdrant_health(base_url, deadline_s=30.0):
            raise RuntimeError(f"Qdrant did not become ready on {base_url} within 30s")
        root_status, root_body, _ = _http_get(f"{base_url}/")
        assert root_status == 200
        assert json.loads(root_body)["version"] == QDRANT_VERSION
        client = QdrantClient(url=base_url, timeout=30)
        try:
            yield (base_url, container_name)
        finally:
            client.close()
    finally:
        # Cleanup: remove the container, volume, network. The
        # --rm flag handles container removal; we add an explicit
        # rm -f for safety.
        if container_id is not None:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                text=True,
            )
        subprocess.run(
            ["docker", "volume", "rm", "-f", volume_name],
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["docker", "network", "rm", network_name],
            capture_output=True,
            text=True,
        )


def _make_artifact(namespace: str = "eric/dev/artifact") -> SourceArtifact:
    now = utc_now()
    return SourceArtifact(
        object_id=generate_ksuid(),
        namespace=namespace,
        title="ART-001 Spike Artifact",
        filename="spike.txt",
        sha256="0" * 64,
        content_type="text/plain",
        size_bytes=100,
        chunker="token-sliding-v1",
        chunk_count=0,
        artifact_state="indexing",
        created_at=now,
        created_epoch=epoch_of(now),
        updated_at=now,
        updated_epoch=epoch_of(now),
        ingestion_metadata={"source": "art001-spike"},
    )


class _CollectionClient(Protocol):
    def collection_exists(self, collection_name: str) -> bool: ...

    def delete_collection(self, collection_name: str) -> Any: ...

    def create_collection(self, collection_name: str, **kwargs: Any) -> Any: ...


def _ensure_collection(client: _CollectionClient, name: str, dim: int) -> None:
    """Create the collection if missing; recreate to a known shape
    to avoid drift. We use 2 collections: the metadata collection
    and the chunks collection (per the existing plane.py layout)."""
    from qdrant_client.http import models as http_models

    if client.collection_exists(name):
        # Recreate to a known shape to avoid drift.
        client.delete_collection(name)
    client.create_collection(
        collection_name=name,
        vectors_config={
            DENSE_VECTOR_NAME: http_models.VectorParams(
                size=dim, distance=http_models.Distance.COSINE
            )
        },
        sparse_vectors_config={SPARSE_VECTOR_NAME: http_models.SparseVectorParams()},
    )


def test_ensure_collection_deletes_an_existing_collection_exactly_once() -> None:
    """Red-proofs the duplicate-delete regression in the setup harness."""
    client = Mock(spec=QdrantClient)
    client.collection_exists.return_value = True

    _ensure_collection(cast(_CollectionClient, client), "art001_duplicate_delete_probe", 4)

    client.delete_collection.assert_called_once_with("art001_duplicate_delete_probe")
    client.create_collection.assert_called_once()


@pytest.fixture(scope="module")
def plane(qdrant_server: tuple[str, str]) -> Iterator[ArtifactPlane]:
    """Build an ArtifactPlane bound to the ephemeral Qdrant
    container. Uses FakeEmbedder (per existing test_artifact.py
    fixture) to avoid the embedding dependency. The plane is
    configured with the same 2-collection layout as the source."""
    from qdrant_client import QdrantClient

    base_url, _ = qdrant_server
    # Bootstrap the collections to match the plane's layout.
    client = QdrantClient(url=base_url, timeout=30)
    dim = DENSE_SIZE
    # Same collection names as the source.
    _ensure_collection(client, "musubi_artifact", dim)
    _ensure_collection(client, "musubi_artifact_chunks", dim)
    client.close()
    # Now build the plane.
    fresh = QdrantClient(url=base_url, timeout=30)
    plane = ArtifactPlane(client=fresh, embedder=FakeEmbedder())
    try:
        yield plane
    finally:
        fresh.close()


def test_spike_setup_health(qdrant_server: tuple[str, str]) -> None:
    """The Qdrant container is up; /readyz returns 200; the
    server version endpoint responds. This is the spike's
    baseline confirmation that we are talking to a real
    persistent Qdrant, not `:memory:`.

    Per Yua 00:28:44: probe via /readyz plus server root
    (version endpoint/log), NOT --version/--help.
    """
    base_url, _ = qdrant_server
    status, _, _ = _http_get(f"{base_url}/readyz")
    assert status in (200, 204), f"real Qdrant not ready at {base_url}/readyz (status={status})"
    root_status, root_body, _ = _http_get(f"{base_url}/")
    assert root_status == 200, (
        f"real Qdrant root not reachable at {base_url}/ (status={root_status})"
    )
    assert json.loads(root_body)["version"] == QDRANT_VERSION


@pytest.fixture
def real_client(qdrant_server: tuple[str, str]) -> Iterator[QdrantClient]:
    base_url, _ = qdrant_server
    client = QdrantClient(url=base_url, timeout=30)
    try:
        yield client
    finally:
        client.close()


@contextlib.contextmanager
def _temporary_collection(client: QdrantClient) -> Iterator[str]:
    from qdrant_client.http import models

    name = f"art001_{secrets.token_hex(6)}"
    client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE),
    )
    try:
        yield name
    finally:
        client.delete_collection(name)


def _point(payload: dict[str, Any] | None = None) -> Any:
    from qdrant_client.http import models

    return models.PointStruct(
        id=str(uuid.uuid4()),
        vector=[1.0, 0.0, 0.0, 0.0],
        payload=payload or {},
    )


def _scroll_payloads(
    client: QdrantClient,
    collection: str,
    *,
    scroll_filter: Any | None = None,
) -> list[dict[str, Any]]:
    rows, _ = client.scroll(
        collection_name=collection,
        scroll_filter=scroll_filter,
        limit=100,
        with_payload=True,
    )
    return [dict(row.payload or {}) for row in rows]


# ---------------------------------------------------------------------------
# Eight-row real-Qdrant matrix
# ---------------------------------------------------------------------------


def test_matrix_1_same_collection_batch_success_is_visible_but_partial_failure_is_unproven(
    real_client: QdrantClient,
) -> None:
    from qdrant_client.http import models

    with _temporary_collection(real_client) as collection:
        first, second = _point({"operation": 1}), _point({"operation": 2})
        results = real_client.batch_update_points(
            collection_name=collection,
            wait=True,
            update_operations=[
                models.UpsertOperation(upsert=models.PointsList(points=[first])),
                models.UpsertOperation(upsert=models.PointsList(points=[second])),
            ],
        )
        assert len(results) == 2
        assert {row["operation"] for row in _scroll_payloads(real_client, collection)} == {1, 2}
        # This proves only the successful path. There is no server-supported,
        # operation-specific fault in this test from which to infer atomicity.


def test_matrix_2_filter_selector_updates_matches_without_reporting_matched_count(
    real_client: QdrantClient,
) -> None:
    from qdrant_client.http import models

    with _temporary_collection(real_client) as collection:
        real_client.upsert(
            collection_name=collection,
            wait=True,
            points=[_point({"version": 1}), _point({"version": 2})],
        )
        result = real_client.set_payload(
            collection_name=collection,
            wait=True,
            payload={"claimed": True},
            points=models.FilterSelector(
                filter=models.Filter(
                    must=[models.FieldCondition(key="version", match=models.MatchValue(value=1))]
                )
            ),
        )
        payloads = _scroll_payloads(real_client, collection)
        assert sum(row.get("claimed") is True for row in payloads) == 1
        assert not hasattr(result, "matched_count")
        assert not hasattr(result, "modified_count")


def test_matrix_3_multiple_connections_provide_no_concurrent_writer_ordering_signal(
    qdrant_server: tuple[str, str], real_client: QdrantClient
) -> None:
    base_url, _ = qdrant_server
    with _temporary_collection(real_client) as collection:
        point_id = str(uuid.uuid4())
        clients = [QdrantClient(url=base_url, timeout=30) for _ in range(2)]
        try:
            for client, writer in zip(clients, ("first", "second"), strict=True):
                client.upsert(
                    collection_name=collection,
                    wait=True,
                    points=[
                        _point_with_id(point_id, {"writer": writer}),
                    ],
                )
            assert _scroll_payloads(real_client, collection) == [{"writer": "second"}]
        finally:
            for client in clients:
                client.close()


def _point_with_id(point_id: str, payload: dict[str, Any]) -> Any:
    from qdrant_client.http import models

    return models.PointStruct(id=point_id, vector=[1.0, 0.0, 0.0, 0.0], payload=payload)


def test_matrix_4_cross_collection_writes_have_a_visible_transaction_boundary(
    real_client: QdrantClient,
) -> None:
    with (
        _temporary_collection(real_client) as metadata,
        _temporary_collection(real_client) as chunks,
    ):
        real_client.upsert(
            collection_name=chunks,
            wait=True,
            points=[_point({"generation": "staged"})],
        )
        assert _scroll_payloads(real_client, chunks) == [{"generation": "staged"}]
        assert _scroll_payloads(real_client, metadata) == []


def test_matrix_5_successful_batch_is_visible_to_the_first_following_scroll(
    real_client: QdrantClient,
) -> None:
    from qdrant_client.http import models

    with _temporary_collection(real_client) as collection:
        point = _point({"published": False})
        real_client.batch_update_points(
            collection_name=collection,
            wait=True,
            update_operations=[
                models.UpsertOperation(upsert=models.PointsList(points=[point])),
                models.SetPayloadOperation(
                    set_payload=models.SetPayload(payload={"published": True}, points=[point.id])
                ),
            ],
        )
        assert _scroll_payloads(real_client, collection) == [{"published": True}]


def test_matrix_6_ordinary_upsert_presence_is_not_a_writer_fence(
    qdrant_server: tuple[str, str], real_client: QdrantClient
) -> None:
    base_url, _ = qdrant_server
    with _temporary_collection(real_client) as collection:
        lease_id = str(uuid.uuid4())
        clients = [QdrantClient(url=base_url, timeout=30) for _ in range(2)]
        try:
            results = [
                client.upsert(
                    collection_name=collection,
                    wait=True,
                    points=[_point_with_id(lease_id, {"owner": owner})],
                )
                for client, owner in zip(clients, ("writer-a", "writer-b"), strict=True)
            ]
            assert all(str(result.status).lower().endswith("completed") for result in results)
            assert _scroll_payloads(real_client, collection)[0]["owner"] in {
                "writer-a",
                "writer-b",
            }
        finally:
            for client in clients:
                client.close()


def test_matrix_7_committed_generation_filter_hides_in_flight_generation(
    real_client: QdrantClient,
) -> None:
    from qdrant_client.http import models

    with _temporary_collection(real_client) as collection:
        real_client.upsert(
            collection_name=collection,
            wait=True,
            points=[
                _point({"artifact_id": "a", "generation": "old", "committed": True}),
                _point({"artifact_id": "a", "generation": "new", "committed": False}),
            ],
        )
        unfiltered = _scroll_payloads(real_client, collection)
        committed = _scroll_payloads(
            real_client,
            collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(key="artifact_id", match=models.MatchValue(value="a")),
                    models.FieldCondition(key="committed", match=models.MatchValue(value=True)),
                ]
            ),
        )
        assert len(unfiltered) == 2
        assert [row["generation"] for row in committed] == ["old"]


@pytest.mark.asyncio
async def test_matrix_8_current_failure_after_chunk_upsert_leaves_chunks_queryable(
    plane: ArtifactPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _make_artifact()
    await plane.create(artifact)
    _fail_next_metadata_publication(plane, monkeypatch)

    failed = await plane.index(artifact, "staged content")

    assert failed.artifact_state == "failed"
    assert [
        chunk.content for chunk in await plane.query_by_artifact(artifact_id=artifact.object_id)
    ] == ["staged content"]


def _fail_next_metadata_publication(plane: ArtifactPlane, monkeypatch: pytest.MonkeyPatch) -> None:
    client = plane._client
    original = client.set_payload
    failed = False

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal failed
        collection = kwargs.get("collection_name")
        if collection == "musubi_artifact" and not failed:
            failed = True
            raise RuntimeError("controlled publication ambiguity")
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "set_payload", fail_once)


# ---------------------------------------------------------------------------
# Eight desired properties: controls execute before each strict current-source red.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="ART-001: repeated index exposes orphan generations")
async def test_property_1_second_successful_index_exposes_exactly_one_committed_generation(
    plane: ArtifactPlane,
) -> None:
    artifact = _make_artifact()
    await plane.create(artifact)
    first = await plane.index(artifact, "same content")
    assert (
        first.chunk_count == len(await plane.query_by_artifact(artifact_id=artifact.object_id)) == 1
    )
    second = await plane.index(artifact, "same content")
    assert (
        second.chunk_count
        == len(await plane.query_by_artifact(artifact_id=artifact.object_id))
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="ART-001: shorter reindex leaves the old tail visible")
async def test_property_2_reindex_from_more_chunks_to_fewer_removes_or_hides_old_tail(
    plane: ArtifactPlane,
) -> None:
    artifact = _make_artifact()
    await plane.create(artifact)
    await plane.index(artifact, " ".join(f"long-{i}" for i in range(900)))
    shortened = await plane.index(artifact, "short")
    visible = await plane.query_by_artifact(artifact_id=artifact.object_id)
    assert len(visible) == shortened.chunk_count == 1
    assert visible[0].content == "short"


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True, reason="ART-001: failed generation is visible beside prior generation"
)
async def test_property_3_failure_before_publish_keeps_only_prior_generation_visible(
    plane: ArtifactPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _make_artifact()
    await plane.create(artifact)
    prior = await plane.index(artifact, "prior committed")
    assert prior.artifact_state == "indexed"
    _fail_next_metadata_publication(plane, monkeypatch)
    failed = await plane.index(artifact, "failed generation")
    assert failed.artifact_state == "failed"
    assert [
        chunk.content for chunk in await plane.query_by_artifact(artifact_id=artifact.object_id)
    ] == ["prior committed"]


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="ART-001: first failed generation remains queryable")
async def test_property_4_first_failed_index_exposes_zero_partial_chunks(
    plane: ArtifactPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _make_artifact()
    await plane.create(artifact)
    _fail_next_metadata_publication(plane, monkeypatch)
    failed = await plane.index(artifact, "never committed")
    assert failed.artifact_state == "failed"
    assert await plane.query_by_artifact(artifact_id=artifact.object_id) == []


class _RendezvousEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.arrivals = 0
        self.all_arrived = asyncio.Event()

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        if texts != [" "]:
            self.arrivals += 1
            if self.arrivals == 2:
                self.all_arrived.set()
            await asyncio.wait_for(self.all_arrived.wait(), timeout=5)
        return await super().embed_dense(texts)


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="ART-001: same-artifact writers are not fenced")
async def test_property_5_deterministic_same_artifact_concurrency_has_one_serialized_winner(
    qdrant_server: tuple[str, str],
) -> None:
    base_url, _ = qdrant_server
    client = QdrantClient(url=base_url, timeout=30)
    rendezvous = _RendezvousEmbedder()
    concurrent_plane = ArtifactPlane(client=client, embedder=rendezvous)
    artifact = _make_artifact()
    try:
        await concurrent_plane.create(artifact)
        results = await asyncio.gather(
            concurrent_plane.index(artifact, "writer alpha"),
            concurrent_plane.index(artifact, "writer beta"),
        )
        visible = await concurrent_plane.query_by_artifact(artifact_id=artifact.object_id)
        contents = {chunk.content for chunk in visible}
        assert all(result.artifact_state == "indexed" for result in results)
        assert len(contents) == len(visible) == 1
    finally:
        client.close()


@pytest.mark.asyncio
async def test_property_6_different_artifact_concurrency_remains_independent(
    plane: ArtifactPlane,
) -> None:
    first, second = _make_artifact(), _make_artifact()
    await plane.create(first)
    await plane.create(second)
    indexed = await asyncio.gather(
        plane.index(first, "first artifact"),
        plane.index(second, "second artifact"),
    )
    assert [row.artifact_state for row in indexed] == ["indexed", "indexed"]
    assert [
        chunk.content for chunk in await plane.query_by_artifact(artifact_id=first.object_id)
    ] == ["first artifact"]
    assert [
        chunk.content for chunk in await plane.query_by_artifact(artifact_id=second.object_id)
    ] == ["second artifact"]


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="ART-001: ambiguous retry creates another generation")
async def test_property_7_retry_after_ambiguous_failure_is_idempotent(
    plane: ArtifactPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _make_artifact()
    await plane.create(artifact)
    _fail_next_metadata_publication(plane, monkeypatch)
    assert (await plane.index(artifact, "retry content")).artifact_state == "failed"
    retried = await plane.index(artifact, "retry content")
    visible = await plane.query_by_artifact(artifact_id=artifact.object_id)
    assert retried.artifact_state == "indexed"
    assert len(visible) == retried.chunk_count == 1


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="ART-001: metadata count diverges from visible chunks")
async def test_property_8_metadata_chunk_count_equals_visible_chunks_after_every_outcome(
    plane: ArtifactPlane,
) -> None:
    artifact = _make_artifact()
    await plane.create(artifact)
    clean = await plane.index(artifact, "clean")
    assert clean.chunk_count == len(await plane.query_by_artifact(artifact_id=artifact.object_id))
    reindexed = await plane.index(artifact, "replacement")
    visible = await plane.query_by_artifact(artifact_id=artifact.object_id)
    assert reindexed.chunk_count == len(visible)


# ---------------------------------------------------------------------------
# Seven wrong-candidate discriminators (test-only reference model).
# ---------------------------------------------------------------------------


def _assert_single_generation(view: list[tuple[str, str]]) -> None:
    assert len({generation for generation, _ in view}) == 1
    assert len({content for _, content in view}) == 1


def _assert_prior_survives_publish_failure(view: list[tuple[str, str]]) -> None:
    assert view == [("prior", "prior-content")]


def _assert_one_serialized_winner(view: list[tuple[str, str]]) -> None:
    _assert_single_generation(view)
    assert len(view) == 1


def test_wrong_candidate_1_deterministic_ids_without_fence_is_rejected() -> None:
    correct = [("winner", "writer-a")]
    wrong = [("writer-a", "writer-a"), ("writer-b", "writer-b")]
    _assert_one_serialized_winner(correct)
    with pytest.raises(AssertionError):
        _assert_one_serialized_winner(wrong)


def test_wrong_candidate_2_delete_before_upsert_without_fence_is_rejected() -> None:
    correct = [("winner", "winner-content")]
    wrong: list[tuple[str, str]] = []
    _assert_one_serialized_winner(correct)
    with pytest.raises(AssertionError):
        _assert_one_serialized_winner(wrong)


def test_wrong_candidate_3_upsert_before_delete_on_publish_failure_is_rejected() -> None:
    correct = [("prior", "prior-content")]
    wrong = [("failed", "failed-content")]
    _assert_prior_survives_publish_failure(correct)
    with pytest.raises(AssertionError):
        _assert_prior_survives_publish_failure(wrong)


def test_wrong_candidate_4_generation_pointer_without_read_filter_is_rejected() -> None:
    correct = [("new", "same-content")]
    wrong = [("old", "same-content"), ("new", "same-content")]
    _assert_single_generation(correct)
    with pytest.raises(AssertionError):
        _assert_single_generation(wrong)


def test_wrong_candidate_5_unfenced_last_writer_wins_switch_is_rejected() -> None:
    correct = [("writer-b", "writer-b")]
    wrong = [("writer-a", "writer-a"), ("writer-b", "writer-b")]
    _assert_one_serialized_winner(correct)
    with pytest.raises(AssertionError):
        _assert_one_serialized_winner(wrong)


def test_wrong_candidate_6_compensating_rollback_deleting_winner_is_rejected() -> None:
    correct = [("winner", "winner-content")]
    wrong: list[tuple[str, str]] = []
    _assert_one_serialized_winner(correct)
    with pytest.raises(AssertionError):
        _assert_one_serialized_winner(wrong)


def test_wrong_candidate_7_bare_gather_without_rendezvous_is_rejected() -> None:
    async def bare_gather_harness() -> None:
        await asyncio.gather(asyncio.sleep(0), asyncio.sleep(0))

    async def rendezvous_harness() -> None:
        event = asyncio.Event()
        event.set()
        await asyncio.gather(event.wait(), event.wait())

    assert "Event" in rendezvous_harness.__code__.co_names
    assert "Event" not in bare_gather_harness.__code__.co_names
