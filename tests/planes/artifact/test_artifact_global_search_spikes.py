"""ART-001 real-Qdrant global-search publication discriminator.

This tests/docs-only spike imports no Musubi production code.  It compares
global exact-K visibility candidates against a digest-pinned Qdrant v1.17.1
server.  Named strict reds execute deliberately wrong candidates; unmarked
tests are controls or bounded positive evidence.
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
from collections.abc import Callable, Iterator, Sequence
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
QUERY = [1.0, 0.0]
EXACT_K = 2
MIN_ALIAS_OBSERVATIONS = 100


def _uuid(value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"musubi:art001:global:{value}"))


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


def _qdrant_platform(machine: str) -> tuple[str, str]:
    normalized = machine.lower()
    if normalized not in PLATFORMS:
        supported = ", ".join(sorted(PLATFORMS))
        raise RuntimeError(
            f"unsupported Qdrant spike architecture {machine!r}; supported: {supported}"
        )
    return PLATFORMS[normalized]


@pytest.fixture(scope="module")
def qdrant_url() -> Iterator[str]:
    machine = platform.machine().lower()
    docker_platform, digest = _qdrant_platform(machine)
    http_port, grpc_port = _free_port(), _free_port()
    while grpc_port == http_port:
        grpc_port = _free_port()
    token = secrets.token_hex(5)
    container = f"art001-global-{token}"
    network = f"art001-global-net-{token}"
    volume = f"art001-global-vol-{token}"
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
def client(qdrant_url: str) -> Iterator[QdrantClient]:
    value = QdrantClient(url=qdrant_url, timeout=20)
    try:
        yield value
    finally:
        value.close()


def _collection(client: QdrantClient, prefix: str) -> str:
    name = f"art001_{prefix}_{secrets.token_hex(5)}"
    client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(size=2, distance=models.Distance.DOT),
    )
    return name


def _point(
    name: str,
    score: float,
    *,
    artifact: str,
    generation: str,
    owner: str,
    published: bool,
    role: str,
) -> models.PointStruct:
    return models.PointStruct(
        id=_uuid(name),
        vector=[score, 0.0],
        payload={
            "name": name,
            "artifact_id": artifact,
            "generation": generation,
            "owner_token": owner,
            "published": published,
            "role": role,
        },
    )


def _upsert(client: QdrantClient, collection: str, points: Sequence[models.PointStruct]) -> None:
    client.upsert(collection_name=collection, points=list(points), wait=True)


def _names(
    client: QdrantClient,
    collection: str,
    *,
    limit: int = EXACT_K,
    query_filter: models.Filter | None = None,
    offset: int | None = None,
) -> list[str]:
    rows = client.query_points(
        collection_name=collection,
        query=QUERY,
        query_filter=query_filter,
        with_payload=True,
        limit=limit,
        offset=offset,
    ).points
    return [str(row.payload["name"]) for row in rows if row.payload is not None]


def _match(**values: Any) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key=key, match=models.MatchValue(value=value))
            for key, value in values.items()
        ]
    )


def _matrix() -> list[models.PointStruct]:
    return [
        _point(
            "stale-high-1",
            0.99,
            artifact="artifact-a",
            generation="old-a",
            owner="old-owner",
            published=False,
            role="stale-high-score",
        ),
        _point(
            "new-staged-a",
            0.98,
            artifact="artifact-a",
            generation="new-a",
            owner="new-owner",
            published=False,
            role="new-staged",
        ),
        _point(
            "losing-owner-a",
            0.97,
            artifact="artifact-a",
            generation="loser-a",
            owner="losing-owner",
            published=False,
            role="losing-owner",
        ),
        _point(
            "stale-high-2",
            0.96,
            artifact="artifact-b",
            generation="old-b",
            owner="old-owner",
            published=False,
            role="stale-high-score",
        ),
        _point(
            "stale-high-3",
            0.95,
            artifact="artifact-c",
            generation="old-c",
            owner="old-owner",
            published=False,
            role="stale-high-score",
        ),
        _point(
            "winning-current-a",
            0.90,
            artifact="artifact-a",
            generation="winning-a",
            owner="winning-owner",
            published=True,
            role="winning-current",
        ),
        _point(
            "current-b",
            0.80,
            artifact="artifact-b",
            generation="current-b",
            owner="stable-owner",
            published=True,
            role="old-committed",
        ),
    ]


HEADS = {
    "artifact-a": ("winning-a", "winning-owner"),
    "artifact-b": ("current-b", "stable-owner"),
}


def _is_current(payload: dict[str, Any]) -> bool:
    expected = HEADS.get(str(payload["artifact_id"]))
    return expected == (payload["generation"], payload["owner_token"])


def _iterative_refill(
    client: QdrantClient,
    collection: str,
    *,
    page_size: int,
    max_candidates: int,
    after_page: Callable[[int], None] | None = None,
) -> tuple[list[str], int]:
    accepted: list[str] = []
    examined = 0
    offset = 0
    while len(accepted) < EXACT_K and examined < max_candidates:
        take = min(page_size, max_candidates - examined)
        rows = client.query_points(
            collection_name=collection,
            query=QUERY,
            with_payload=True,
            limit=take,
            offset=offset,
        ).points
        if not rows:
            break
        for row in rows:
            payload = dict(row.payload or {})
            if _is_current(payload):
                accepted.append(str(payload["name"]))
                if len(accepted) == EXACT_K:
                    break
        examined += len(rows)
        offset += len(rows)
        if after_page is not None:
            after_page(examined)
        if len(rows) < take:
            break
    return accepted, examined


def _alias_switch(client: QdrantClient, alias: str, target: str) -> None:
    client.update_collection_aliases(
        change_aliases_operations=[
            models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias)),
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(collection_name=target, alias_name=alias)
            ),
        ]
    )


def _alias_target(client: QdrantClient, alias: str) -> str:
    matches = [
        item.collection_name for item in client.get_aliases().aliases if item.alias_name == alias
    ]
    assert len(matches) == 1
    return matches[0]


def _validate_alias_observations(
    result: dict[str, Any],
    *,
    expected_old: list[str],
    expected_new: list[str],
    minimum: int = MIN_ALIAS_OBSERVATIONS,
) -> None:
    errors = list(result["errors"])
    if errors:
        raise AssertionError(f"alias reader errors: {errors}")
    seen = list(result["seen"])
    for names in seen:
        if names == []:
            raise AssertionError("alias reader observed an empty gap")
        if len(names) != EXACT_K:
            raise AssertionError(f"alias reader observed wrong result length: {names}")
        if names not in (expected_old, expected_new):
            raise AssertionError(f"alias reader observed a mixed or unexpected set: {names}")
    if len(seen) < minimum:
        raise AssertionError(f"alias reader sample floor not met: {len(seen)} < {minimum}")
    if expected_old not in seen:
        raise AssertionError("alias reader never observed the old snapshot")
    if expected_new not in seen:
        raise AssertionError("alias reader never observed the new snapshot")


def _stop_reader(reader: subprocess.Popen[bytes] | None) -> None:
    if reader is None or reader.poll() is not None:
        return
    reader.terminate()
    try:
        reader.wait(timeout=5)
    except subprocess.TimeoutExpired:
        reader.kill()
        reader.wait(timeout=5)


READER = r"""
import json, sys, time
from pathlib import Path
from qdrant_client import QdrantClient
url, alias, deadline, ready, output = sys.argv[1:]
client = QdrantClient(url=url, timeout=20)
seen = []
errors = []
while time.time() < float(deadline):
    try:
        rows = client.query_points(collection_name=alias, query=[1.0,0.0], with_payload=True, limit=2).points
        seen.append([row.payload["name"] for row in rows])
        if len(seen) == 1: Path(ready).touch()
    except Exception as exc:
        errors.append(type(exc).__name__)
with open(output, "w", encoding="utf-8") as stream:
    json.dump({"seen": seen, "errors": errors}, stream)
client.close()
"""


ACTIVATOR = r"""
import os, sys
from qdrant_client import QdrantClient
from qdrant_client.http import models
url, alias, target, mode = sys.argv[1:]
client = QdrantClient(url=url, timeout=20)
if mode == "before": os._exit(31)
client.update_collection_aliases(change_aliases_operations=[
    models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias)),
    models.CreateAliasOperation(create_alias=models.CreateAlias(collection_name=target, alias_name=alias)),
])
if mode == "ambiguous": os._exit(32)
assert any(item.alias_name == alias and item.collection_name == target for item in client.get_aliases().aliases)
os._exit(33)
"""


def _snapshot_points(prefix: str) -> list[models.PointStruct]:
    first = "old-committed-a" if prefix == "old" else "new-winning-a"
    generation = "old-a" if prefix == "old" else "new-a"
    owner = "old-owner" if prefix == "old" else "new-owner"
    return [
        _point(
            first,
            0.90 if prefix == "old" else 0.95,
            artifact="artifact-a",
            generation=generation,
            owner=owner,
            published=True,
            role="old-committed" if prefix == "old" else "winning-current",
        ),
        _point(
            "stable-current-b",
            0.80,
            artifact="artifact-b",
            generation="stable-b",
            owner="stable-owner",
            published=True,
            role="old-committed",
        ),
    ]


def _seed_alias_pair(client: QdrantClient) -> tuple[str, str, str]:
    old, new = _collection(client, "old"), _collection(client, "new")
    alias = f"art001_live_{secrets.token_hex(5)}"
    _upsert(client, old, _snapshot_points("old"))
    _upsert(client, new, _snapshot_points("new"))
    client.update_collection_aliases(
        change_aliases_operations=[
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(collection_name=old, alias_name=alias)
            )
        ]
    )
    return old, new, alias


def test_real_server_and_cross_arch_pins_are_exact(qdrant_url: str) -> None:
    assert len(DIGEST_AMD64) == len(DIGEST_ARM64) == 64
    assert DIGEST_AMD64 != DIGEST_ARM64
    with urllib.request.urlopen(f"{qdrant_url}/", timeout=2) as response:
        assert json.load(response)["version"] == QDRANT_VERSION


def test_supported_architecture_selects_exact_platform_and_digest() -> None:
    assert _qdrant_platform("amd64") == ("linux/amd64", DIGEST_AMD64)
    assert _qdrant_platform("x86_64") == ("linux/amd64", DIGEST_AMD64)
    assert _qdrant_platform("arm64") == ("linux/arm64", DIGEST_ARM64)
    assert _qdrant_platform("aarch64") == ("linux/arm64", DIGEST_ARM64)


def test_unknown_architecture_fails_closed_without_skip_or_fallback() -> None:
    with pytest.raises(RuntimeError, match="unsupported Qdrant spike architecture 'riscv64'"):
        _qdrant_platform("riscv64")


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"seen": [["old-a", "stable-b"]] * 100, "errors": ["UnexpectedResponse"]}, "errors"),
        ({"seen": [["old-a", "stable-b"], []] * 50, "errors": []}, "empty gap"),
        ({"seen": [["old-a"]] * 100, "errors": []}, "wrong result length"),
        (
            {"seen": [["old-a", "new-a"]] * 100, "errors": []},
            "mixed or unexpected set",
        ),
        ({"seen": [["new-a", "stable-b"]] * 100, "errors": []}, "never observed the old"),
        ({"seen": [["old-a", "stable-b"]] * 100, "errors": []}, "never observed the new"),
        (
            {
                "seen": [["old-a", "stable-b"]] * 49 + [["new-a", "stable-b"]] * 50,
                "errors": [],
            },
            "sample floor not met",
        ),
    ],
    ids=["error", "gap", "wrong-length", "mixed", "missing-old", "missing-new", "floor"],
)
def test_alias_observation_validator_rejects_every_visibility_defect(
    result: dict[str, Any], message: str
) -> None:
    with pytest.raises(AssertionError, match=message):
        _validate_alias_observations(
            result,
            expected_old=["old-a", "stable-b"],
            expected_new=["new-a", "stable-b"],
        )


def test_adversarial_matrix_names_every_required_visibility_state() -> None:
    roles = {str(point.payload["role"]) for point in _matrix() if point.payload is not None}
    assert roles == {
        "old-committed",
        "new-staged",
        "winning-current",
        "stale-high-score",
        "losing-owner",
    }


def test_parent_head_iterative_refill_is_exact_and_bounded_when_quiescent(
    client: QdrantClient,
) -> None:
    collection = _collection(client, "refill")
    try:
        _upsert(client, collection, _matrix())
        names, examined = _iterative_refill(
            client, collection, page_size=2, max_candidates=len(_matrix())
        )
        assert names == ["winning-current-a", "current-b"]
        assert examined <= len(_matrix())
    finally:
        client.delete_collection(collection)


def test_per_chunk_published_filter_is_exact_when_activation_is_quiescent(
    client: QdrantClient,
) -> None:
    collection = _collection(client, "flags")
    try:
        _upsert(client, collection, _matrix())
        assert _names(client, collection, query_filter=_match(published=True)) == [
            "winning-current-a",
            "current-b",
        ]
    finally:
        client.delete_collection(collection)


def test_complete_collection_alias_cutover_preserves_exact_k_for_concurrent_reader(
    client: QdrantClient, qdrant_url: str, tmp_path: Path
) -> None:
    old, new, alias = _seed_alias_pair(client)
    reader: subprocess.Popen[bytes] | None = None
    ready = tmp_path / "reader.ready"
    output = tmp_path / "reader.json"
    expected_old = ["old-committed-a", "stable-current-b"]
    expected_new = ["new-winning-a", "stable-current-b"]
    try:
        reader = subprocess.Popen(
            [
                sys.executable,
                "-c",
                READER,
                qdrant_url,
                alias,
                str(time.time() + 2.0),
                str(ready),
                str(output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        _alias_switch(client, alias, new)
        stdout, stderr = reader.communicate(timeout=20)
        assert reader.returncode == 0, (stdout, stderr)
        result = json.loads(output.read_text())
        _validate_alias_observations(
            result,
            expected_old=expected_old,
            expected_new=expected_new,
        )
    finally:
        _stop_reader(reader)
        client.delete_collection(old)
        client.delete_collection(new)


@pytest.mark.parametrize(
    ("mode", "expected_target", "returncode"),
    [("before", "old", 31), ("ambiguous", "new", 32), ("after", "new", 33)],
)
def test_process_death_before_during_and_after_activation_reconciles_by_alias_readback(
    client: QdrantClient,
    qdrant_url: str,
    mode: str,
    expected_target: str,
    returncode: int,
) -> None:
    old, new, alias = _seed_alias_pair(client)
    try:
        worker = subprocess.run(
            [sys.executable, "-c", ACTIVATOR, qdrant_url, alias, new, mode],
            capture_output=True,
            timeout=20,
        )
        assert worker.returncode == returncode, worker.stderr
        expected = old if expected_target == "old" else new
        assert _alias_target(client, alias) == expected
        assert len(_names(client, alias)) == EXACT_K
        if expected == new:
            client.delete_collection(old)
            assert _names(client, alias) == ["new-winning-a", "stable-current-b"]
        else:
            client.delete_collection(new)
            assert _names(client, alias) == ["old-committed-a", "stable-current-b"]
    finally:
        for collection in (old, new):
            if client.collection_exists(collection):
                client.delete_collection(collection)


def test_activation_retry_readback_and_cleanup_are_deterministic(client: QdrantClient) -> None:
    old, new, alias = _seed_alias_pair(client)
    try:
        _alias_switch(client, alias, new)
        assert _alias_target(client, alias) == new
        _alias_switch(client, alias, new)
        assert _alias_target(client, alias) == new
        client.delete_collection(old)
        assert _names(client, alias) == ["new-winning-a", "stable-current-b"]
    finally:
        if client.collection_exists(new):
            client.delete_collection(new)


def test_complete_alias_candidate_meets_exact_k_safety_and_recall(client: QdrantClient) -> None:
    old, new, alias = _seed_alias_pair(client)
    try:
        staged = _collection(client, "staged_loser")
        _upsert(
            client,
            staged,
            [
                _point(
                    "uncommitted-high",
                    1.0,
                    artifact="artifact-a",
                    generation="loser-a",
                    owner="losing-owner",
                    published=False,
                    role="losing-owner",
                )
            ],
        )
        _alias_switch(client, alias, new)
        names = _names(client, alias)
        assert names == ["new-winning-a", "stable-current-b"]
        assert "uncommitted-high" not in names
        assert len(names) == EXACT_K
    finally:
        for collection in (old, new, staged):
            if client.collection_exists(collection):
                client.delete_collection(collection)


def _reject_fabricated_server_crash_claim(injection: str) -> None:
    if injection == "client death after accepted request":
        raise ValueError("does not inject a crash inside Qdrant alias consensus")


def test_client_death_is_not_mislabeled_as_a_qdrant_snapshot_or_server_crash() -> None:
    with pytest.raises(ValueError, match="inside Qdrant alias consensus"):
        _reject_fabricated_server_crash_claim("client death after accepted request")


# Named wrong-candidate reds. Controls above are deliberately unmarked.
@pytest.mark.xfail(
    strict=True, reason="wrong candidate: bounded overfetch preserves exact-K recall"
)
def test_red_naive_bounded_overfetch_loses_current_exact_k(client: QdrantClient) -> None:
    collection = _collection(client, "overfetch")
    try:
        _upsert(client, collection, _matrix())
        candidates = client.query_points(
            collection_name=collection,
            query=QUERY,
            with_payload=True,
            limit=EXACT_K * 2,
        ).points
        names = [
            str(row.payload["name"])
            for row in candidates
            if row.payload is not None and _is_current(dict(row.payload))
        ]
        assert names == ["winning-current-a", "current-b"]
    finally:
        client.delete_collection(collection)


@pytest.mark.xfail(
    strict=True, reason="candidate boundary: offset refill is not a concurrent Qdrant snapshot"
)
def test_red_iterative_refill_cannot_claim_concurrent_snapshot(client: QdrantClient) -> None:
    collection = _collection(client, "refill_mutating")
    mutated = False

    def move_current_ahead(examined: int) -> None:
        nonlocal mutated
        if examined == 2 and not mutated:
            mutated = True
            _upsert(
                client,
                collection,
                [
                    _point(
                        "winning-current-a",
                        1.01,
                        artifact="artifact-a",
                        generation="winning-a",
                        owner="winning-owner",
                        published=True,
                        role="winning-current",
                    )
                ],
            )

    try:
        _upsert(client, collection, _matrix())
        names, examined = _iterative_refill(
            client,
            collection,
            page_size=2,
            max_candidates=len(_matrix()),
            after_page=move_current_ahead,
        )
        assert examined <= len(_matrix())
        assert names == ["winning-current-a", "current-b"]
    finally:
        client.delete_collection(collection)


@pytest.mark.xfail(
    strict=True,
    reason="candidate boundary: deactivate-old-first survives a crash without false negatives",
)
def test_red_flag_activation_crash_after_deactivate_loses_current_exact_k(
    client: QdrantClient,
) -> None:
    collection = _collection(client, "flag_gap")
    try:
        _upsert(client, collection, _matrix())
        client.set_payload(
            collection_name=collection,
            payload={"published": False},
            points=[_uuid("winning-current-a")],
            wait=True,
        )
        assert _names(client, collection, query_filter=_match(published=True)) == [
            "winning-current-a",
            "current-b",
        ]
    finally:
        client.delete_collection(collection)


@pytest.mark.xfail(
    strict=True,
    reason="candidate boundary: activate-new-first cannot expose an uncommitted generation",
)
def test_red_flag_activation_exposes_new_before_old_is_fenced(client: QdrantClient) -> None:
    collection = _collection(client, "flag_overlap")
    try:
        _upsert(client, collection, _matrix())
        client.set_payload(
            collection_name=collection,
            payload={"published": True},
            points=[_uuid("new-staged-a")],
            wait=True,
        )
        assert _names(client, collection, query_filter=_match(published=True)) == [
            "winning-current-a",
            "current-b",
        ]
    finally:
        client.delete_collection(collection)


@pytest.mark.xfail(
    strict=True,
    reason="wrong alias candidate: partial collection copy preserves global exact-K recall",
)
def test_red_per_artifact_alias_promotion_loses_unaffected_current_rows(
    client: QdrantClient,
) -> None:
    old = _collection(client, "alias_old")
    partial = _collection(client, "alias_partial")
    alias = f"art001_partial_{secrets.token_hex(5)}"
    try:
        _upsert(client, old, _snapshot_points("old"))
        _upsert(client, partial, _snapshot_points("new")[:1])
        client.update_collection_aliases(
            change_aliases_operations=[
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(collection_name=old, alias_name=alias)
                )
            ]
        )
        _alias_switch(client, alias, partial)
        assert _names(client, alias) == ["new-winning-a", "stable-current-b"]
    finally:
        client.delete_collection(old)
        client.delete_collection(partial)


@pytest.mark.xfail(
    strict=True, reason="wrong claim: client death injects a crash inside Qdrant alias consensus"
)
def test_red_ambiguous_client_death_proves_mid_server_crash_atomicity() -> None:
    _reject_fabricated_server_crash_claim("client death after accepted request")
    assert True


@pytest.mark.xfail(
    strict=True, reason="wrong candidate: split delete/create alias calls preserve gap-free reads"
)
def test_red_non_atomic_split_alias_switch_exposes_a_real_query_gap(
    client: QdrantClient, qdrant_url: str
) -> None:
    old, new, alias = _seed_alias_pair(client)
    reader = QdrantClient(url=qdrant_url, timeout=20)
    expected_old = ["old-committed-a", "stable-current-b"]
    expected_new = ["new-winning-a", "stable-current-b"]
    result: dict[str, Any] = {"seen": [expected_old] * 50, "errors": []}
    try:
        client.update_collection_aliases(
            change_aliases_operations=[
                models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias))
            ]
        )
        try:
            gap = _names(reader, alias)
            result["seen"].append(gap)
        except Exception as exc:
            result["errors"].append(type(exc).__name__)
        client.update_collection_aliases(
            change_aliases_operations=[
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(collection_name=new, alias_name=alias)
                )
            ]
        )
        result["seen"].extend([expected_new] * 50)
        _validate_alias_observations(
            result,
            expected_old=expected_old,
            expected_new=expected_new,
        )
    finally:
        reader.close()
        client.delete_collection(old)
        client.delete_collection(new)
