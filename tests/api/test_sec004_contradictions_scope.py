"""SEC-004 (C3) P0 — contradictions omitted-namespace scrolls the whole fleet.

Discoverer: Eric. Source-confirmed by Yua (router). Red tests: Aoi.

GET /v1/contradictions is Depends(require_auth()) — ORDINARY auth, not operator — despite a
docstring claiming "operator scope." require_auth reads the namespace from the query
(auth.py:48); when it is OMITTED, scroll_filter is None and the handler scrolls the ENTIRE
musubi_concept collection: every tenant's contradictions, for any valid token.

Also (RET-007 class): `except Exception: return items=[]` — a Qdrant failure becomes an
empty 200, indistinguishable from "no contradictions."

xfail(strict=True) = asserts the SECURE behaviour, fails today, flips green when fixed.
All content synthetic; no live memory.

    uv run pytest tests/api/test_sec004_contradictions_scope.py -v
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from qdrant_client import QdrantClient, models
from starlette.testclient import TestClient

from musubi.settings import Settings
from tests.api.conftest import mint_token

ROUTE = "/v1/contradictions"
CONCEPT = "musubi_concept"


def _seed_contradiction(qdrant: QdrantClient, namespace: str) -> str:
    """Insert one synthetic concept row that marks a contradiction, in `namespace`."""
    oid = "".join(c for c in uuid.uuid4().hex if c.isalnum())[:27].ljust(27, "0")
    other = "".join(c for c in uuid.uuid4().hex if c.isalnum())[:27].ljust(27, "0")
    # dimensionality must match the collection; read it off the live collection config
    info = qdrant.get_collection(CONCEPT)
    vparams = info.config.params.vectors
    vector: dict[str, list[float]] | list[float]
    if isinstance(vparams, dict):  # named vectors
        vector = {name: [0.0] * p.size for name, p in vparams.items()}
    else:
        assert vparams is not None
        vector = [0.0] * vparams.size
    qdrant.upsert(
        CONCEPT,
        points=[
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector=cast("Any", vector),
                payload={
                    "object_id": oid,
                    "namespace": namespace,
                    "contradicts": [other],
                    "state": "matured",
                },
            )
        ],
    )
    return oid


@pytest.fixture
def seeded(qdrant: QdrantClient) -> dict[str, str]:
    """One contradiction in tenant A's namespace, one in tenant B's."""
    return {
        "a": _seed_contradiction(qdrant, "eric/claude-code/concept"),
        "b": _seed_contradiction(qdrant, "mallory/evil/concept"),
    }


def _ordinary(api_settings: Settings, ns: str) -> str:
    return mint_token(api_settings, scopes=[f"{ns}:r"], presence=ns.rsplit("/", 1)[0])


def _operator(api_settings: Settings) -> str:
    return mint_token(api_settings, scopes=["operator", "**:r"], presence="ops/operator")


def test_no_token_must_be_401(client: TestClient) -> None:
    r = client.get(ROUTE)
    assert r.status_code == 401, f"unauthenticated contradictions returned {r.status_code}"


@pytest.mark.xfail(
    strict=True,
    reason="SEC-004: ordinary token + omitted namespace scrolls the fleet — fix pending",
)
def test_ordinary_token_omitted_namespace_must_be_403(
    client: TestClient, api_settings: Settings, seeded: dict[str, str]
) -> None:
    # ordinary token, NO ?namespace= — currently scrolls every tenant's concepts
    token = _ordinary(api_settings, "eric/claude-code/concept")
    r = client.get(ROUTE, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, (
        f"omitted-namespace contradictions returned {r.status_code} for an ORDINARY token "
        f"— a fleet-wide scroll under non-operator auth"
    )


def test_operator_omitted_namespace_succeeds_cross_namespace(
    client: TestClient, api_settings: Settings, seeded: dict[str, str]
) -> None:
    """Operator + omitted namespace is the legitimate cross-namespace case (feature).

    NOT xfail — the fix must still let an OPERATOR fan out. Both synthetic rows (A and B)
    must be visible.
    """
    r = client.get(ROUTE, headers={"Authorization": f"Bearer {_operator(api_settings)}"})
    assert r.status_code == 200, f"operator fanout failed: {r.status_code} {r.text[:200]}"
    ids = {it["object_id"] for it in r.json().get("items", [])}
    assert seeded["a"] in ids and seeded["b"] in ids, (
        "operator cross-namespace scroll must see BOTH namespaces' contradictions"
    )


def test_ordinary_token_own_namespace_returns_only_own(
    client: TestClient, api_settings: Settings, seeded: dict[str, str]
) -> None:
    ns = "eric/claude-code/concept"
    token = _ordinary(api_settings, ns)
    r = client.get(ROUTE, params={"namespace": ns}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"own-namespace contradictions failed: {r.status_code}"
    got = {it["namespace"] for it in r.json().get("items", [])}
    # CONTROL (already passes): an authorized own-namespace read returns only own rows.
    # Supplying the namespace puts it in the query where require_auth CAN see it — so this
    # path is already correct, and the fix must NOT break it.
    assert got <= {ns}, f"own-namespace query leaked other namespaces: {got}"


def test_ordinary_token_foreign_namespace_must_be_403(
    client: TestClient, api_settings: Settings, seeded: dict[str, str]
) -> None:
    # token authorized on A, explicitly requests B's namespace
    token = _ordinary(api_settings, "eric/claude-code/concept")
    r = client.get(
        ROUTE,
        params={"namespace": "mallory/evil/concept"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # CONTROL (already passes): supplying a FOREIGN namespace lands in the query where
    # require_auth sees it and correctly rejects. The leak is ONLY the omitted-namespace
    # path, not this one — an important narrowing of the vulnerability.
    assert r.status_code == 403, (
        f"foreign-namespace contradictions returned {r.status_code} — expected 403"
    )


@pytest.mark.xfail(
    strict=True, reason="SEC-004/RET-007: backend failure must not become empty 200 — fix pending"
)
def test_backend_failure_must_not_be_empty_200(
    client: TestClient, api_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # force the qdrant scroll to raise; the handler currently swallows it -> items=[] 200

    def _boom(*a: object, **k: object) -> Any:
        raise RuntimeError("qdrant is down")

    # patch the client the route uses so scroll raises
    monkeypatch.setattr(QdrantClient, "scroll", _boom, raising=True)
    r = client.get(
        ROUTE,
        params={"namespace": "eric/claude-code/concept"},
        headers={"Authorization": f"Bearer {_ordinary(api_settings, 'eric/claude-code/concept')}"},
    )
    # SECURE: a backend outage is an error, never clean-looking empty data
    assert r.status_code >= 500, (
        f"backend failure returned {r.status_code} with body {r.text[:120]} — an outage "
        f"was reported as empty success (RET-007 class)"
    )
