"""SEC-003 (C2) P0 — namespace outside the query string bypasses scope auth.

Discoverer: Eric. Source-confirmed by Yua (router). Red tests: Aoi.

require_auth reads the namespace it authorizes ONLY from the query string
(auth.py:48 `request.query_params.get(namespace_qs_param)`). On routes whose namespace
arrives via Form / Path / Body, ns=None and the scope check is defanged — a valid token
for one tenant acts on another tenant's namespace.

AFFECTED (full inventory in the slice):
  - POST /v1/artifacts  upload_artifact  (writes_artifact.py:37)  namespace = Form(...)
  - GET  /v1/namespaces/{namespace_path}/stats  (namespaces.py:58)  namespace = Path(...)

xfail(strict=True) = asserts the SECURE behaviour, fails today, flips green when fixed.
Synthetic content only.

    uv run pytest tests/api/test_sec003_namespace_scope.py -v
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from starlette.testclient import TestClient

from musubi.settings import Settings
from tests.api.conftest import mint_token

UPLOAD = "/v1/artifacts"


def _tenant_b(api_settings: Settings) -> str:
    """A valid token that is authorized ONLY on mallory/evil — never on eric/*."""
    return mint_token(api_settings, scopes=["mallory/evil/artifact:rw"], presence="mallory/evil")


def _multipart(namespace: str) -> dict[str, Any]:
    return {
        "namespace": (None, namespace),
        "title": (None, "sec003 probe"),
        "content_type": (None, "text/markdown"),
        "file": ("probe.md", io.BytesIO(b"# heading\nsynthetic body"), "text/markdown"),
    }


@pytest.mark.xfail(strict=True, reason="SEC-003: Form namespace bypasses write scope — fix pending")
def test_upload_cross_tenant_namespace_must_be_403(
    client: TestClient, api_settings: Settings
) -> None:
    # tenant B's token uploads INTO tenant A's namespace via the Form field
    r = client.post(
        UPLOAD,
        files=_multipart("eric/claude-code/artifact"),
        headers={"Authorization": f"Bearer {_tenant_b(api_settings)}"},
    )
    assert r.status_code == 403, (
        f"upload to a foreign namespace returned {r.status_code} — Form namespace was "
        f"never authorized (auth read an empty query param)"
    )


def test_upload_own_namespace_still_succeeds(client: TestClient, api_settings: Settings) -> None:
    """Feature preservation: an authorized upload to one's OWN namespace must work.

    NOT xfail — the fix must authorize the Form namespace, not forbid all uploads.
    """
    token = mint_token(api_settings, scopes=["mallory/evil/artifact:rw"], presence="mallory/evil")
    r = client.post(
        UPLOAD,
        files=_multipart("mallory/evil/artifact"),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201, 202), (
        f"authorized own-namespace upload failed: {r.status_code} {r.text[:200]}"
    )


def test_upload_no_token_must_be_401(client: TestClient) -> None:
    r = client.post(UPLOAD, files=_multipart("eric/claude-code/artifact"))
    assert r.status_code == 401, f"unauthenticated upload returned {r.status_code}"


@pytest.mark.xfail(
    strict=True, reason="SEC-003: Path namespace stats bypasses read scope — fix pending"
)
def test_namespace_stats_cross_tenant_must_be_403(
    client: TestClient, api_settings: Settings
) -> None:
    # tenant B reads stats for tenant A's namespace; the value is a PATH param, so the
    # namespace_qs_param="namespace_path" query lookup is empty and auth checks nothing.
    path = "eric%2Fclaude-code%2Fepisodic"
    r = client.get(
        f"/v1/namespaces/{path}/stats",
        headers={"Authorization": f"Bearer {_tenant_b(api_settings)}"},
    )
    assert r.status_code == 403, (
        f"cross-tenant namespace stats returned {r.status_code} — Path namespace was "
        f"never authorized"
    )


def test_namespace_stats_own_namespace_still_succeeds(
    client: TestClient, api_settings: Settings
) -> None:
    """Feature preservation (declared in the Test Contract): a token scoped EXACTLY to the
    target namespace must read its own stats and get a well-formed body.

    NOT xfail — the fix must authorize the Path namespace, not forbid all stats. Token is
    scoped to exactly the namespace being read (read access), nothing broader.
    """
    target = "mallory/evil/episodic"
    token = mint_token(api_settings, scopes=[f"{target}:r"], presence="mallory/evil")
    path = target.replace("/", "%2F")
    r = client.get(f"/v1/namespaces/{path}/stats", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"own-namespace stats failed: {r.status_code} {r.text[:200]}"
    body = r.json()
    assert body.get("namespace") == target, f"namespace echo wrong: {body.get('namespace')!r}"
    assert isinstance(body.get("counts"), dict), "counts must be a dict per NamespaceStats"


def test_namespace_stats_no_token_must_be_401(client: TestClient) -> None:
    path = "eric%2Fclaude-code%2Fepisodic"
    r = client.get(f"/v1/namespaces/{path}/stats")
    assert r.status_code == 401, f"unauthenticated stats returned {r.status_code}"
