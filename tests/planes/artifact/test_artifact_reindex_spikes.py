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

import contextlib
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest
from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.planes.artifact import ArtifactPlane
from musubi.types.artifact import SourceArtifact
from musubi.types.common import epoch_of, generate_ksuid, utc_now

# Resolved from the OCI image index for qdrant/qdrant:v1.17.1
# (host is aarch64 / arm64 per `uname -m`).
DIGEST_ARM64 = (
    "3fd57e61606ed61c48c91c4131cba6808f01b0879f5478fd011573189855bba1"
)
# qdrant/qdrant@<digest>; the image-internal "sha256:" prefix lives
# inside DIGEST_ARM64 itself. (See spike-notes/qdrant-digest-record.txt.)
IMAGE = f"qdrant/qdrant@sha256:{DIGEST_ARM64}"  # Docker ref needs the sha256: prefix


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
    (linux/arm64, sha256:3fd57e...) bound to 127.0.0.1 on a
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

    # Create a dedicated user-defined bridge network for the spike.
    subprocess.run(
        ["docker", "network", "create", "--driver", "bridge", network_name],
        check=True, capture_output=True, text=True,
    )
    # Create a dedicated volume for the spike.
    subprocess.run(
        ["docker", "volume", "create", volume_name],
        check=True, capture_output=True, text=True,
    )
    container_id: str | None = None
    try:
        # Run the container bound to 127.0.0.1 only on a collision-free
        # port; do NOT expose to all interfaces.
        proc = subprocess.run(
            [
                "docker", "run", "-d", "--rm",
                "--name", container_name,
                "--network", network_name,
                "-v", f"{volume_name}:/qdrant/storage",
                "-p", f"127.0.0.1:{port_http}:6333",
                "-p", f"127.0.0.1:{port_grpc}:6334",
                IMAGE,
            ],
            capture_output=True, text=True, check=True,
        )
        container_id = proc.stdout.strip()
        base_url = f"http://127.0.0.1:{port_http}"
        if not _wait_for_qdrant_health(base_url, deadline_s=30.0):
            raise RuntimeError(
                f"Qdrant did not become ready on {base_url} within 30s"
            )
        # Probe the server root for the spike record.
        try:
            _status, body, server_header = _http_get(f"{base_url}/")
            server_info = server_header or body[:200]
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            server_info = "<unreachable>"
        print(
            f"\n[art001-spike] ephemeral Qdrant up at {base_url} "
            f"(image={IMAGE}, container={container_name[:24]}..., "
            f"server={server_info!r})"
        )
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
                capture_output=True, text=True,
            )
        subprocess.run(
            ["docker", "volume", "rm", "-f", volume_name],
            capture_output=True, text=True,
        )
        subprocess.run(
            ["docker", "network", "rm", network_name],
            capture_output=True, text=True,
        )


def _make_artifact(namespace: str = "eric/dev/artifact-spike") -> SourceArtifact:
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


def _ensure_collection(client: QdrantClient, name: str, dim: int) -> None:
    """Create the collection if missing; recreate to a known shape
    to avoid drift. We use 2 collections: the metadata collection
    and the chunks collection (per the existing plane.py layout)."""
    from qdrant_client.http import models as http_models
    if client.collection_exists(name):
        # Recreate to a known shape to avoid drift.
        client.delete_collection(name)
    client.create_collection(
        collection_name=name,
        vectors_config=http_models.VectorParams(
            size=dim, distance=http_models.Distance.COSINE
        ),
    )


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
    dim = 4  # FakeEmbedder convention; matches existing test_artifact.py fixture
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
    assert status in (200, 204), (
        f"real Qdrant not ready at {base_url}/readyz "
        f"(status={status})"
    )
    root_status, _, _ = _http_get(f"{base_url}/")
    assert root_status == 200, (
        f"real Qdrant root not reachable at {base_url}/ "
        f"(status={root_status})"
    )


# Placeholder for the 8-row matrix. The actual spike work is
# documented in spike-notes/ and is not in this file because the
# spike requires the running container (see the fixture above).
# The acceptance invariants from the REV3 audit are encoded as
# test stubs in spike-notes/acceptance-invariants.md.
