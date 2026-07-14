"""ART-001 tests-only Qdrant v1.17.1 OCC and crash discriminator.

No production code is imported.  The candidate seam lives entirely in this
file and talks to a digest-pinned real Qdrant through independent clients and
independent Python interpreters.
"""

from __future__ import annotations

import json
import platform
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import QdrantClient
from qdrant_client.http import models

QDRANT_VERSION = "1.17.1"
DIGEST_AMD64 = "cd3e42737c684ee516ae5533218be93fd5288f41d0a466ed18dbdc22ef52a000"
DIGEST_ARM64 = "3fd57e61606ed61c48c91c4131cba6808f01b0879f5478fd011573189855bba1"
PLATFORMS = {
    "amd64": ("linux/amd64", DIGEST_AMD64),
    "x86_64": ("linux/amd64", DIGEST_AMD64),
    "aarch64": ("linux/arm64", DIGEST_ARM64),
    "arm64": ("linux/arm64", DIGEST_ARM64),
}
VECTOR = [1.0, 0.0]
ARTIFACT = "artifact-occ-spike"


def _uuid(kind: str, value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"musubi:art001:{kind}:{value}"))


def _head_id(artifact: str = ARTIFACT) -> str:
    return _uuid("head", artifact)


def _op_id(owner: str) -> str:
    return _uuid("operation", owner)


def _chunk_id(owner: str, index: int = 0) -> str:
    return _uuid("chunk", f"{owner}:{index}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(url: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/readyz", timeout=1) as response:
                if response.status in (200, 204):
                    return
        except OSError:
            pass
        time.sleep(0.25)
    raise AssertionError(f"Qdrant did not become ready: {url}")


@pytest.fixture(scope="module")
def qdrant_url() -> Iterator[str]:
    machine = platform.machine().lower()
    docker_platform, digest = PLATFORMS[machine]
    http_port, grpc_port = _free_port(), _free_port()
    while grpc_port == http_port:
        grpc_port = _free_port()
    token = secrets.token_hex(5)
    container = f"art001-occ-{token}"
    network = f"art001-occ-net-{token}"
    volume = f"art001-occ-vol-{token}"
    image = f"qdrant/qdrant@sha256:{digest}"
    subprocess.run(["docker", "network", "create", network], check=True, capture_output=True)
    subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--platform",
                docker_platform,
                "--name",
                container,
                "--network",
                network,
                "-v",
                f"{volume}:/qdrant/storage",
                "-p",
                f"127.0.0.1:{http_port}:6333",
                "-p",
                f"127.0.0.1:{grpc_port}:6334",
                image,
            ],
            check=True,
            capture_output=True,
        )
        url = f"http://127.0.0.1:{http_port}"
        _wait_ready(url)
        with urllib.request.urlopen(f"{url}/", timeout=2) as response:
            assert json.load(response)["version"] == QDRANT_VERSION
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


@pytest.fixture
def store(qdrant_url: str) -> Iterator[tuple[QdrantClient, str]]:
    client = QdrantClient(url=qdrant_url, timeout=20)
    collection = f"art001_occ_{secrets.token_hex(5)}"
    client.create_collection(
        collection_name=collection,
        vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE),
    )
    try:
        yield client, collection
    finally:
        client.delete_collection(collection)
        client.close()


def _payload_filter(**values: Any) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key=key, match=models.MatchValue(value=value))
            for key, value in values.items()
        ]
    )


def _upsert(client: QdrantClient, collection: str, point_id: str, payload: dict[str, Any]) -> Any:
    return client.upsert(
        collection_name=collection,
        points=[models.PointStruct(id=point_id, vector=VECTOR, payload=payload)],
        wait=True,
    )


def _seed_head(client: QdrantClient, collection: str, owner: str = "seed-owner") -> dict[str, Any]:
    payload = {
        "kind": "head",
        "artifact_id": ARTIFACT,
        "version": 1,
        "generation": "generation-1",
        "owner_token": owner,
    }
    _upsert(client, collection, _head_id(), payload)
    return payload


def _read(client: QdrantClient, collection: str, point_id: str) -> dict[str, Any] | None:
    rows = client.retrieve(collection_name=collection, ids=[point_id], with_payload=True)
    return dict(rows[0].payload or {}) if rows else None


def _scroll(client: QdrantClient, collection: str, **where: Any) -> list[dict[str, Any]]:
    rows, _ = client.scroll(
        collection_name=collection,
        scroll_filter=_payload_filter(**where),
        with_payload=True,
        with_vectors=False,
        limit=100,
    )
    return [dict(row.payload or {}) for row in rows]


def _stage(client: QdrantClient, collection: str, generation: str, owner: str) -> None:
    _upsert(
        client,
        collection,
        _op_id(owner),
        {
            "kind": "operation",
            "artifact_id": ARTIFACT,
            "generation": generation,
            "owner_token": owner,
            "base_version": 1,
            "target_version": 2,
            "status": "started",
        },
    )
    _upsert(
        client,
        collection,
        _chunk_id(owner),
        {
            "kind": "chunk",
            "artifact_id": ARTIFACT,
            "generation": generation,
            "owner_token": owner,
            "text": generation,
        },
    )


def _publish(
    client: QdrantClient,
    collection: str,
    generation: str,
    owner: str,
    *,
    expected_version: int = 1,
    expected_owner: str = "seed-owner",
) -> Any:
    return client.upsert(
        collection_name=collection,
        points=[
            models.PointStruct(
                id=_head_id(),
                vector=VECTOR,
                payload={
                    "kind": "head",
                    "artifact_id": ARTIFACT,
                    "version": 2,
                    "generation": generation,
                    "owner_token": owner,
                },
            )
        ],
        update_filter=_payload_filter(version=expected_version, owner_token=expected_owner),
        update_mode=models.UpdateMode.UPDATE_ONLY,
        wait=True,
    )


def _reconcile(client: QdrantClient, collection: str, owner: str) -> str:
    operation = _read(client, collection, _op_id(owner))
    assert operation is not None
    head = _read(client, collection, _head_id())
    committed = bool(
        head
        and head.get("generation") == operation["generation"]
        and head.get("owner_token") == owner
    )
    status = "committed" if committed else "aborted"
    if not committed:
        client.delete(
            collection_name=collection,
            points_selector=_payload_filter(
                kind="chunk",
                artifact_id=ARTIFACT,
                generation=operation["generation"],
                owner_token=owner,
            ),
            wait=True,
        )
    operation["status"] = status
    _upsert(client, collection, _op_id(owner), operation)
    return status


WORKER = r"""
import json, os, sys, time
from qdrant_client import QdrantClient
from qdrant_client.http import models
url, collection, generation, owner, mode, barrier, output = sys.argv[1:]
artifact = "artifact-occ-spike"
import uuid
uid = lambda kind, value: str(uuid.uuid5(uuid.NAMESPACE_URL, f"musubi:art001:{kind}:{value}"))
client = QdrantClient(url=url, timeout=20)
filt = lambda **v: models.Filter(must=[models.FieldCondition(key=k, match=models.MatchValue(value=x)) for k,x in v.items()])
put = lambda pid,payload: client.upsert(collection_name=collection, points=[models.PointStruct(id=pid, vector=[1.0,0.0], payload=payload)], wait=True)
op = {"kind":"operation","artifact_id":artifact,"generation":generation,"owner_token":owner,"base_version":1,"target_version":2,"status":"started"}
put(uid("operation", owner), op)
if mode == "before_stage": os._exit(31)
put(uid("chunk", f"{owner}:0"), {"kind":"chunk","artifact_id":artifact,"generation":generation,"owner_token":owner,"text":generation})
if mode == "after_stage": os._exit(32)
if mode == "before_cleanup": os._exit(35)
while time.time() < float(barrier): time.sleep(.002)
result = client.upsert(collection_name=collection, points=[models.PointStruct(id=uid("head", artifact), vector=[1.0,0.0], payload={"kind":"head","artifact_id":artifact,"version":2,"generation":generation,"owner_token":owner})], update_filter=filt(version=1, owner_token="seed-owner"), update_mode=models.UpdateMode.UPDATE_ONLY, wait=True)
if mode == "before_response": os._exit(33)
head = client.retrieve(collection_name=collection, ids=[uid("head", artifact)], with_payload=True)[0].payload
if mode == "after_ambiguous_response": os._exit(34)
with open(output, "w", encoding="utf-8") as f: json.dump({"status":str(result.status),"operation_id":result.operation_id,"head":head}, f)
client.close()
"""


def _worker(
    url: str,
    collection: str,
    generation: str,
    owner: str,
    mode: str,
    barrier: float,
    output: Path,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            WORKER,
            url,
            collection,
            generation,
            owner,
            mode,
            str(barrier),
            str(output),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_real_server_and_cross_arch_pins_are_exact(qdrant_url: str) -> None:
    assert len(DIGEST_AMD64) == len(DIGEST_ARM64) == 64
    assert DIGEST_AMD64 != DIGEST_ARM64
    with urllib.request.urlopen(f"{qdrant_url}/", timeout=2) as response:
        assert json.load(response)["version"] == QDRANT_VERSION


def test_two_process_conditional_publish_has_one_readback_winner(
    store: tuple[QdrantClient, str], qdrant_url: str, tmp_path: Path
) -> None:
    client, collection = store
    _seed_head(client, collection)
    barrier = time.time() + 1.0
    specs = [("generation-a", "owner-a"), ("generation-b", "owner-b")]
    workers = [
        _worker(
            qdrant_url, collection, generation, owner, "race", barrier, tmp_path / f"{owner}.json"
        )
        for generation, owner in specs
    ]
    errors = [worker.communicate(timeout=30) for worker in workers]
    assert [worker.returncode for worker in workers] == [0, 0], errors
    results = [json.loads((tmp_path / f"{owner}.json").read_text()) for _, owner in specs]
    assert [result["status"] for result in results] == ["completed", "completed"]
    assert len({result["operation_id"] for result in results}) == 2
    head = _read(client, collection, _head_id())
    assert head is not None
    winners = [
        (generation, owner)
        for generation, owner in specs
        if (head["generation"], head["owner_token"]) == (generation, owner)
    ]
    assert len(winners) == 1
    loser = next(owner for generation, owner in specs if (generation, owner) not in winners)
    assert _reconcile(client, collection, loser) == "aborted"
    assert _read(client, collection, _head_id()) == head
    assert _scroll(client, collection, kind="chunk", owner_token=loser) == []


def test_update_only_equality_missing_point_and_stale_retry(
    store: tuple[QdrantClient, str],
) -> None:
    client, collection = store
    _seed_head(client, collection)
    miss = _publish(client, collection, "wrong", "wrong", expected_version=0)
    assert str(miss.status) == "completed"
    assert _read(client, collection, _head_id())["version"] == 1  # type: ignore[index]
    missing_id = _uuid("missing", collection)
    result = client.upsert(
        collection_name=collection,
        points=[models.PointStruct(id=missing_id, vector=VECTOR, payload={"version": 2})],
        update_filter=_payload_filter(version=1),
        update_mode=models.UpdateMode.UPDATE_ONLY,
        wait=True,
    )
    assert str(result.status) == "completed"
    assert _read(client, collection, missing_id) is None
    _publish(client, collection, "winner-generation", "winner-owner")
    _publish(client, collection, "stale-generation", "stale-owner")
    assert _read(client, collection, _head_id())["owner_token"] == "winner-owner"  # type: ignore[index]


def test_fresh_owner_token_blocks_version_reuse_aba(store: tuple[QdrantClient, str]) -> None:
    client, collection = store
    _seed_head(client, collection)
    _publish(client, collection, "generation-2", "owner-2")
    # Deliberately rewind the numeric version, but retain a fresh owner token.
    _upsert(
        client,
        collection,
        _head_id(),
        {
            "kind": "head",
            "artifact_id": ARTIFACT,
            "version": 1,
            "generation": "generation-3",
            "owner_token": "owner-3",
        },
    )
    _publish(client, collection, "stale-generation", "stale-owner")
    head = _read(client, collection, _head_id())
    assert (head["generation"], head["owner_token"]) == ("generation-3", "owner-3")  # type: ignore[index]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("before_stage", "aborted"),
        ("after_stage", "aborted"),
        ("before_response", "committed"),
        ("after_ambiguous_response", "committed"),
        ("before_cleanup", "aborted"),
    ],
)
def test_process_death_reconciles_deterministically(
    store: tuple[QdrantClient, str], qdrant_url: str, tmp_path: Path, mode: str, expected: str
) -> None:
    client, collection = store
    _seed_head(client, collection)
    owner, generation = f"owner-{mode}", f"generation-{mode}"
    worker = _worker(
        qdrant_url, collection, generation, owner, mode, time.time() + 0.25, tmp_path / mode
    )
    _, stderr = worker.communicate(timeout=30)
    assert worker.returncode in {31, 32, 33, 34, 35}, stderr.decode()
    assert _reconcile(client, collection, owner) == expected
    chunks = _scroll(client, collection, kind="chunk", owner_token=owner)
    assert len(chunks) == (1 if expected == "committed" else 0)


def test_head_then_generation_read_is_per_artifact_consistent(
    store: tuple[QdrantClient, str],
) -> None:
    client, collection = store
    _seed_head(client, collection)
    _stage(client, collection, "generation-2", "owner-2")
    _publish(client, collection, "generation-2", "owner-2")
    _stage(client, collection, "generation-loser", "owner-loser")
    head = _read(client, collection, _head_id())
    assert head is not None
    rows = _scroll(
        client,
        collection,
        kind="chunk",
        artifact_id=ARTIFACT,
        generation=head["generation"],
        owner_token=head["owner_token"],
    )
    assert [row["generation"] for row in rows] == ["generation-2"]


def _infer_legacy_generation(metadata_count: int, rows: list[dict[str, Any]]) -> str:
    del metadata_count, rows
    raise ValueError("legacy ownership is unknowable; rebuild from canonical blob")


def test_legacy_mixed_generation_requires_canonical_blob_rebuild() -> None:
    rows = [{"chunk_index": 0, "text": "old"}, {"chunk_index": 0, "text": "new"}]
    with pytest.raises(ValueError, match="canonical blob"):
        _infer_legacy_generation(metadata_count=1, rows=rows)


def _require_independent_interpreter(start_method: str) -> None:
    if start_method == "fork":
        raise ValueError("fork after a parent Qdrant client is not an independent-client proof")


# Named wrong-candidate reds. Controls above are deliberately unmarked.
@pytest.mark.xfail(strict=True, reason="wrong candidate: UpdateResult status identifies the winner")
def test_red_completed_status_cannot_identify_the_race_winner(
    store: tuple[QdrantClient, str],
) -> None:
    client, collection = store
    _seed_head(client, collection)
    winner = _publish(client, collection, "winner", "winner-owner")
    loser = _publish(client, collection, "loser", "loser-owner")
    assert [str(winner.status), str(loser.status)].count("completed") == 1


@pytest.mark.xfail(strict=True, reason="wrong candidate: version equality alone prevents ABA")
def test_red_version_only_cannot_distinguish_reused_version(
    store: tuple[QdrantClient, str],
) -> None:
    client, collection = store
    _seed_head(client, collection)
    _upsert(
        client,
        collection,
        _head_id(),
        {
            "kind": "head",
            "artifact_id": ARTIFACT,
            "version": 1,
            "generation": "fresh-generation",
            "owner_token": "fresh-owner",
        },
    )
    client.upsert(
        collection_name=collection,
        points=[
            models.PointStruct(
                id=_head_id(),
                vector=VECTOR,
                payload={
                    "kind": "head",
                    "artifact_id": ARTIFACT,
                    "version": 2,
                    "generation": "stale-generation",
                    "owner_token": "stale-owner",
                },
            )
        ],
        update_filter=_payload_filter(version=1),
        update_mode=models.UpdateMode.UPDATE_ONLY,
        wait=True,
    )
    assert _read(client, collection, _head_id())["owner_token"] == "fresh-owner"  # type: ignore[index]


@pytest.mark.xfail(
    strict=True, reason="wrong candidate: artifact-wide rollback preserves a concurrent winner"
)
def test_red_artifact_wide_cleanup_cannot_preserve_winner(
    store: tuple[QdrantClient, str],
) -> None:
    client, collection = store
    _stage(client, collection, "winner-generation", "winner-owner")
    _stage(client, collection, "loser-generation", "loser-owner")
    client.delete(
        collection_name=collection,
        points_selector=_payload_filter(kind="chunk", artifact_id=ARTIFACT),
        wait=True,
    )
    assert len(_scroll(client, collection, kind="chunk", owner_token="winner-owner")) == 1


@pytest.mark.xfail(strict=True, reason="wrong candidate: default UPSERT is a missing-point CAS")
def test_red_default_upsert_inserts_when_the_conditional_point_is_missing(
    store: tuple[QdrantClient, str],
) -> None:
    client, collection = store
    missing_id = _uuid("default-upsert", collection)
    client.upsert(
        collection_name=collection,
        points=[models.PointStruct(id=missing_id, vector=VECTOR, payload={"version": 2})],
        update_filter=_payload_filter(version=1),
        wait=True,
    )
    assert _read(client, collection, missing_id) is None


@pytest.mark.xfail(
    strict=True, reason="wrong candidate: one head atomically filters a namespace-wide vector query"
)
def test_red_head_pointer_does_not_solve_namespace_wide_visibility(
    store: tuple[QdrantClient, str],
) -> None:
    client, collection = store
    _seed_head(client, collection)
    _stage(client, collection, "committed-generation", "committed-owner")
    _publish(client, collection, "committed-generation", "committed-owner")
    _stage(client, collection, "inflight-generation", "inflight-owner")
    points = client.query_points(
        collection_name=collection,
        query=VECTOR,
        query_filter=_payload_filter(kind="chunk", artifact_id=ARTIFACT),
        with_payload=True,
        limit=10,
    ).points
    payloads = [point.payload for point in points]
    assert all(payload is not None for payload in payloads)
    assert {payload["generation"] for payload in payloads if payload is not None} == {
        "committed-generation"
    }


@pytest.mark.xfail(
    strict=True, reason="wrong candidate: metadata count assigns legacy chunk ownership"
)
def test_red_metadata_count_cannot_partition_mixed_legacy_rows() -> None:
    metadata_count = 1
    legacy_rows = ["old-index-0", "new-index-0"]
    assert len(legacy_rows) == metadata_count


@pytest.mark.xfail(
    strict=True, reason="wrong harness: forked children are independent-client contention proof"
)
def test_red_fork_after_parent_client_is_rejected() -> None:
    _require_independent_interpreter("fork")
