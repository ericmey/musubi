"""Artifact upload/download size handling (public-repo validation, musubi finding 2).

Upload used to ``await file.read()`` the whole body and download used to
``read_bytes()`` the whole blob, so neither had an application-level bound.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from fastapi.testclient import TestClient

from musubi.settings import Settings
from tests.api.conftest import mint_token

NS = "eric/claude-code/artifact"


def _upload(client: TestClient, token: str, payload: bytes) -> object:
    return client.post(
        "/v1/artifacts",
        headers={"Authorization": f"Bearer {token}"},
        data={"namespace": NS, "title": "t", "content_type": "application/octet-stream"},
        files={"file": ("f.bin", io.BytesIO(payload), "application/octet-stream")},
    )


def _files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def test_upload_over_limit_is_refused_and_leaves_nothing_behind(
    client: TestClient, api_settings: Settings
) -> None:
    object.__setattr__(api_settings, "artifact_max_bytes", 1024)
    token = mint_token(api_settings, scopes=[f"{NS}:rw"])

    r = _upload(client, token, b"x" * 1025)

    assert r.status_code == 413  # type: ignore[attr-defined]
    assert r.json()["error"]["code"] == "CONTENT_TOO_LARGE"  # type: ignore[attr-defined]
    # No blob, and no orphaned staging file.
    assert _files_under(api_settings.artifact_blob_path) == []
    listed = client.get(
        "/v1/artifacts", params={"namespace": NS}, headers={"Authorization": f"Bearer {token}"}
    )
    assert listed.json()["items"] == []


def test_upload_at_limit_round_trips_bytes_size_and_hash(
    client: TestClient, api_settings: Settings
) -> None:
    object.__setattr__(api_settings, "artifact_max_bytes", 3 * 1024 * 1024)
    token = mint_token(api_settings, scopes=[f"{NS}:rw"])
    # Multi-chunk payload (upload streams in 1 MiB chunks), exactly at the limit.
    payload = bytes(range(256)) * (3 * 1024 * 1024 // 256)

    r = _upload(client, token, payload)

    assert r.status_code == 202  # type: ignore[attr-defined]
    body = r.json()  # type: ignore[attr-defined]
    assert body["size_bytes"] == len(payload)
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()
    blob = client.get(
        f"/v1/artifacts/{body['object_id']}/blob",
        params={"namespace": NS},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert blob.status_code == 200
    assert blob.content == payload
    assert not any(
        ".staging" in str(p) and p.suffix == ".part"
        for p in _files_under(api_settings.artifact_blob_path)
    )


def _staging_parts(root: Path) -> list[Path]:
    staging = root / ".staging"
    return list(staging.glob("*.part")) if staging.exists() else []


def _post_expecting_failure(client: TestClient, token: str) -> None:
    # A failure after the bytes are written surfaces as a server error. Whether the
    # TestClient re-raises it or returns a 5xx depends on its configuration; either
    # way, the assertion that matters is about the staging directory afterwards.
    try:
        r = _upload(client, token, b"payload")
        assert r.status_code >= 500  # type: ignore[attr-defined]
    except RuntimeError:
        pass


def test_failure_in_plane_create_leaves_no_staging_file(
    client: TestClient, api_settings: Settings, monkeypatch: object
) -> None:
    from musubi.planes.artifact import ArtifactPlane

    async def boom(self: object, *a: object, **k: object) -> object:
        raise RuntimeError("injected plane.create failure")

    monkeypatch.setattr(ArtifactPlane, "create", boom)  # type: ignore[attr-defined]
    token = mint_token(api_settings, scopes=[f"{NS}:rw"])

    _post_expecting_failure(client, token)

    assert _staging_parts(api_settings.artifact_blob_path) == []


def test_failure_in_final_replace_leaves_no_staging_file(
    client: TestClient, api_settings: Settings, monkeypatch: object
) -> None:
    import musubi.api.routers.writes_artifact as wa

    def boom(src: object, dst: object) -> None:
        raise RuntimeError("injected os.replace failure")

    monkeypatch.setattr(wa.os, "replace", boom)  # type: ignore[attr-defined]
    token = mint_token(api_settings, scopes=[f"{NS}:rw"])

    _post_expecting_failure(client, token)

    assert _staging_parts(api_settings.artifact_blob_path) == []
