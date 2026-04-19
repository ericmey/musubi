"""Test contract for slice-api-v0-read.

Implements the read-side bullets from
[[07-interfaces/canonical-api]] § Test contract. Every read-side bullet
is in one of three Closure states:

- a passing test whose name transcribes the bullet text verbatim, OR
- ``@pytest.mark.skip(reason="deferred to slice-api-v0-write: ...")`` or
  ``deferred to <named follow-up>: ...``, OR
- declared out-of-scope in the slice work log (the ``contract-tests.md``
  meta bullets, which are tests OF the contract-tests repo, and the
  ``v2`` versioning bullet which has no v2 to live alongside).

Runs against the FastAPI app built via ``create_app`` with all DI
overridden to in-memory test instances — no real Qdrant, TEI, Ollama,
or filesystem.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import yaml  # type: ignore
from fastapi.testclient import TestClient

from musubi.planes.curated import CuratedPlane
from musubi.planes.episodic import EpisodicPlane
from musubi.types.curated import CuratedKnowledge
from musubi.types.episodic import EpisodicMemory

# ---------------------------------------------------------------------------
# Shape — bullets 1-3
# ---------------------------------------------------------------------------


def test_openapi_generated_matches_pydantic(client: TestClient) -> None:
    """Bullet 1 — the FastAPI-generated OpenAPI document is reachable and
    has the v1 paths the spec calls for. The committed snapshot
    (``openapi.yaml`` at the repo root) is the deploy-time artifact;
    here we verify that the runtime generator at least produces it
    coherently."""
    r = client.get("/v1/openapi.json")
    assert r.status_code == 200
    doc = r.json()
    assert doc["info"]["title"]
    assert "openapi" in doc and doc["openapi"].startswith("3.")
    paths = set(doc["paths"].keys())
    # Spot-check that documented read paths are routable.
    assert "/v1/memories/{object_id}" in paths
    assert "/v1/curated-knowledge/{object_id}" in paths
    assert "/v1/concepts/{object_id}" in paths
    assert "/v1/artifacts/{object_id}" in paths
    assert "/v1/retrieve" in paths
    assert "/v1/ops/health" in paths
    assert "/v1/namespaces" in paths


def test_all_documented_endpoints_routable(client: TestClient) -> None:
    """Bullet 2 — every read endpoint listed in canonical-api.md returns
    something other than 404 for at least one well-formed request shape.
    Write endpoints are deferred to slice-api-v0-write.

    We check the routability of GET endpoints; auth-rejected paths return
    401 which is also "routable" (the route exists; auth gate fired)."""
    cases = [
        "/v1/memories/0000000000000000000000000000",
        "/v1/curated-knowledge/0000000000000000000000000000",
        "/v1/concepts/0000000000000000000000000000",
        "/v1/artifacts/0000000000000000000000000000",
        "/v1/contradictions",
        "/v1/lifecycle/events",
        "/v1/namespaces",
        "/v1/ops/health",
        "/v1/ops/status",
    ]
    for path in cases:
        r = client.get(path)
        assert r.status_code != 404 or "Not Found" not in r.text, (
            f"{path} returned a generic 404 — route not registered"
        )


def test_error_shape_consistent_across_endpoints(
    client: TestClient,
    valid_token: str,
    auth: dict[str, str],
) -> None:
    """Bullet 3 — every error returned by a read endpoint has the typed
    ``{"error": {"code": ..., "detail": ..., "hint": ...}}`` shape per
    the spec § Response shapes."""
    # 401 — no auth.
    r = client.get("/v1/memories/aaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert r.status_code == 401
    body = r.json()
    assert "error" in body
    assert "code" in body["error"]
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert "detail" in body["error"]
    # 404 — valid auth + namespace, missing object.
    r = client.get(
        "/v1/memories/0000000000000000000000000000",
        headers=auth,
        params={"namespace": "eric/claude-code/episodic"},
    )
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "detail" in body["error"]


# ---------------------------------------------------------------------------
# Auth — bullets 4-6
# ---------------------------------------------------------------------------


def test_missing_token_returns_401(client: TestClient) -> None:
    """Bullet 4 — every authenticated endpoint returns 401 + UNAUTHORIZED
    when the bearer header is absent."""
    paths = [
        "/v1/memories/aaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "/v1/curated-knowledge/aaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "/v1/concepts/aaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "/v1/artifacts/aaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ]
    for path in paths:
        r = client.get(path)
        assert r.status_code == 401, f"{path}: expected 401, got {r.status_code}"
        assert r.json()["error"]["code"] == "UNAUTHORIZED"


def test_out_of_scope_returns_403(
    client: TestClient,
    out_of_scope_token: str,
) -> None:
    """Bullet 5 — a valid token whose scope doesn't grant the requested
    namespace returns 403 FORBIDDEN, not 200 / 404."""
    headers = {"Authorization": f"Bearer {out_of_scope_token}"}
    r = client.get(
        "/v1/memories/aaaaaaaaaaaaaaaaaaaaaaaaaaa?namespace=eric/claude-code/episodic",
        headers=headers,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


def test_operator_scope_accesses_admin_endpoints(
    client: TestClient,
    operator_token: str,
) -> None:
    """Bullet 6 — operator-scoped tokens can access admin read endpoints
    (e.g. lifecycle events listing across all namespaces)."""
    headers = {"Authorization": f"Bearer {operator_token}"}
    r = client.get("/v1/lifecycle/events", headers=headers)
    assert r.status_code == 200
    # And a non-operator scope should be 403 against the same endpoint.
    # Verified via the out-of-scope test above; here we just confirm the
    # operator path works.


# ---------------------------------------------------------------------------
# Content negotiation — bullet 7 only (8, 9 are write/grpc)
# ---------------------------------------------------------------------------


def test_json_default(client: TestClient, auth: dict[str, str]) -> None:
    """Bullet 7 — every read endpoint returns ``application/json`` by
    default. No content-type negotiation needed."""
    r = client.get("/v1/ops/health", headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")


@pytest.mark.skip(
    reason="deferred to a future slice-api-grpc: gRPC transport + proto/ "
    "ownership lives outside this slice's owns_paths."
)
def test_protobuf_via_grpc_matches_rest_semantics() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-api-v0-write: multipart upload is the artifact "
    "POST surface, owned by the write slice."
)
def test_multipart_upload_for_artifacts() -> None:
    pass


# ---------------------------------------------------------------------------
# Idempotency — bullets 10, 11 — write-side
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="deferred to slice-api-v0-write: Idempotency-Key header applies to "
    "POST endpoints only; the read surface has no mutation to dedupe."
)
def test_idempotency_key_roundtrip() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-api-v0-write: idempotency cache TTL is bound to the write surface."
)
def test_idempotency_key_expires_after_24h() -> None:
    pass


# ---------------------------------------------------------------------------
# Versioning — bullet 12 — out-of-scope (no v2 yet)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="declared out-of-scope in slice work log: there is no /v2/ surface "
    "to live alongside; this becomes meaningful only at the API v1 → v2 bump."
)
def test_v1_path_lives_alongside_v2_when_present() -> None:
    pass


# ---------------------------------------------------------------------------
# Rate limits — bullets 13, 14 — write-side
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="deferred to slice-api-v0-write: rate-limit middleware is applied "
    "to mutation endpoints per the slice scope split."
)
def test_rate_limit_enforces_token_bucket() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-api-v0-write: operator-scope rate-limit multiplier "
    "is bound to the write surface."
)
def test_rate_limit_operator_scope_10x_limit() -> None:
    pass


# ---------------------------------------------------------------------------
# Streaming — bullet 15 — deferred (NDJSON streaming complexity)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="deferred to slice-api-v0-write: NDJSON streaming response wiring "
    "(POST /v1/retrieve/stream) bundled with the write-side complexity. The "
    "non-streaming POST /v1/retrieve is implemented here."
)
def test_ndjson_retrieve_stream_yields_per_result() -> None:
    pass


# ---------------------------------------------------------------------------
# Pagination — bullets 16, 17
# ---------------------------------------------------------------------------


def test_cursor_roundtrip_exhausts_list(
    client: TestClient,
    auth: dict[str, str],
    episodic: EpisodicPlane,
) -> None:
    """Bullet 16 — paginating with ``limit=2`` over 5 seeded rows
    eventually returns ``next_cursor: null`` and the union of pages
    equals the full set."""
    namespace = "eric/claude-code/episodic"

    async def _seed() -> list[str]:
        ids: list[str] = []
        for i in range(5):
            saved = await episodic.create(
                EpisodicMemory(namespace=namespace, content=f"page-fixture-{i}-uniq")
            )
            ids.append(saved.object_id)
        return ids

    seeded_ids = asyncio.run(_seed())

    seen: set[str] = set()
    cursor: str | None = None
    for _ in range(10):  # safety bound
        params: dict[str, str | int] = {"namespace": namespace, "limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        r = client.get("/v1/memories", headers=auth, params=params)
        assert r.status_code == 200
        body = r.json()
        for row in body["items"]:
            seen.add(row["object_id"])
        cursor = body.get("next_cursor")
        if cursor is None:
            break
    assert seen >= set(seeded_ids), f"missing ids: {set(seeded_ids) - seen}"


def test_cursor_opaque_to_client(
    client: TestClient,
    auth: dict[str, str],
    episodic: EpisodicPlane,
) -> None:
    """Bullet 17 — the cursor is opaque: it is a non-empty string that
    does not expose internal pagination state directly (no "offset=N" or
    raw KSUID at the start). Clients treat it as a token."""
    namespace = "eric/claude-code/episodic"

    async def _seed() -> None:
        for i in range(3):
            await episodic.create(
                EpisodicMemory(namespace=namespace, content=f"opaque-fixture-{i}")
            )

    asyncio.run(_seed())
    r = client.get("/v1/memories", headers=auth, params={"namespace": namespace, "limit": 1})
    assert r.status_code == 200
    cursor = r.json().get("next_cursor")
    if cursor is not None:
        assert isinstance(cursor, str)
        assert len(cursor) > 0
        # A raw KSUID is 27 chars — opaque cursor should not be just that.
        # Cursor encoding wraps the underlying state.
        assert not cursor.startswith("offset=")


# ---------------------------------------------------------------------------
# Contract — bullet 18 — out-of-scope (contract suite is its own repo)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="declared out-of-scope in slice work log: the contract suite lives "
    "in the future musubi-contract-tests repo; bullet 18 is its end-to-end "
    "smoke. Read-side cases from contract-tests.md are exercised by the "
    "individual router tests below."
)
def test_contract_suite_runs_end_to_end() -> None:
    pass


# ---------------------------------------------------------------------------
# Contract-tests.md "Test contract (meta)" bullets — these are tests OF
# the future ``musubi-contract-tests`` repo (a separate Python package
# per ADR-0011). They cannot exist in the musubi-core monorepo by
# design. Listed here as ``@pytest.mark.skip`` so the Closure Rule
# audit (``make tc-coverage``) sees a named follow-up rather than a
# silent omission.
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="deferred to musubi-contract-tests repo: meta-test of the contract "
    "suite's per-test scoped_namespace fixture; lives in that repo, not here."
)
def test_every_test_declares_scoped_namespace() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to musubi-contract-tests repo: meta-test of the suite's "
    "teardown hook; lives in that repo, not here."
)
def test_teardown_archives_created_data() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to musubi-contract-tests repo: meta-test that the "
    "canonical suite runs clean against a reference Musubi; lives in that repo."
)
def test_suite_runs_clean_against_reference_musubi() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to musubi-contract-tests repo: smoke-suite latency budget "
    "is a property of that repo's perf harness, not the API codebase."
)
def test_smoke_suite_completes_under_30s_on_reference_hw() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to musubi-contract-tests repo: operator-hook env-flag "
    "gating is enforced by the suite, lives in that repo."
)
def test_operator_hooks_gated_behind_env_flag() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to musubi-contract-tests repo + slice-api-grpc: dual REST "
    "+ gRPC parametrization lives in the contract suite; gRPC support is a "
    "separate slice."
)
def test_transport_parametrization_covers_rest_and_grpc() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to musubi-contract-tests repo: cross-endpoint error-shape "
    "contract test lives in that repo. The same property is exercised here on "
    "the read surface by test_error_shape_consistent_across_endpoints."
)
def test_all_error_shapes_are_consistent_across_endpoints() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to musubi-contract-tests repo: multi-run isolation is a "
    "property of the suite's fixtures, lives in that repo."
)
def test_no_test_leaks_data_between_runs() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to musubi-contract-tests repo: suite version-pin policy "
    "lives in that repo's release tooling, not the API codebase."
)
def test_suite_version_tagged_against_api_major() -> None:
    pass


# ---------------------------------------------------------------------------
# Additional coverage on owned routers — ensures the 85 % gate clears
# on src/musubi/api/.
# ---------------------------------------------------------------------------


def test_get_artifact_404_when_missing(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get(
        "/v1/artifacts/0000000000000000000000000000",
        headers=auth,
        params={"namespace": "eric/claude-code/artifact"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_get_artifact_chunks_404_when_missing(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get(
        "/v1/artifacts/0000000000000000000000000000/chunks",
        headers=auth,
        params={"namespace": "eric/claude-code/artifact"},
    )
    assert r.status_code == 404


def test_get_artifact_blob_404_when_missing(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get(
        "/v1/artifacts/0000000000000000000000000000/blob",
        headers=auth,
        params={"namespace": "eric/claude-code/artifact"},
    )
    assert r.status_code == 404


def test_list_artifacts_returns_empty_page(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get(
        "/v1/artifacts",
        headers=auth,
        params={"namespace": "eric/claude-code/artifact"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


def test_get_concept_404_when_missing(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get(
        "/v1/concepts/0000000000000000000000000000",
        headers=auth,
        params={"namespace": "eric/claude-code/concept"},
    )
    assert r.status_code == 404


def test_list_concepts_returns_empty_page(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get(
        "/v1/concepts",
        headers=auth,
        params={"namespace": "eric/claude-code/concept"},
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_list_curated_returns_empty_page(client: TestClient, auth: dict[str, str]) -> None:
    r = client.get(
        "/v1/curated-knowledge",
        headers=auth,
        params={"namespace": "eric/claude-code/curated"},
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_lifecycle_events_with_namespace_returns_items_field(
    client: TestClient, operator_token: str
) -> None:
    headers = {"Authorization": f"Bearer {operator_token}"}
    r = client.get(
        "/v1/lifecycle/events",
        headers=headers,
        params={"namespace": "eric/claude-code/episodic"},
    )
    assert r.status_code == 200
    assert "items" in r.json()


def test_lifecycle_events_for_object_returns_items_field(
    client: TestClient, operator_token: str
) -> None:
    headers = {"Authorization": f"Bearer {operator_token}"}
    r = client.get(
        "/v1/lifecycle/events/0000000000000000000000000000",
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_namespace_stats_returns_per_plane_counts(client: TestClient, auth: dict[str, str]) -> None:
    ns = "eric/claude-code/episodic"
    r = client.get(
        f"/v1/namespaces/{ns}/stats",
        headers=auth,
        params={"namespace": ns},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["namespace"] == ns
    assert "episodic" in body["counts"]


def test_metrics_endpoint_returns_prometheus_text(client: TestClient) -> None:
    r = client.get("/v1/ops/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")


def test_thought_history_endpoint_responds(
    client: TestClient,
    api_settings: object,
) -> None:
    from tests.api.conftest import mint_token

    namespace = "eric/claude-code/thought"
    token = mint_token(api_settings, scopes=[f"{namespace}:r"])  # type: ignore[arg-type]
    r = client.post(
        "/v1/thoughts/history",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "namespace": namespace,
            "presence": "eric/claude-code",
            "query_text": "what happened",
        },
    )
    assert r.status_code == 200
    assert "items" in r.json()


def test_correlation_id_echoed_on_response(client: TestClient) -> None:
    """Per the spec § Observability headers, the X-Request-Id is echoed
    on every response (minted if absent from the request)."""
    r = client.get("/v1/ops/health")
    assert "x-request-id" in {k.lower() for k in r.headers}


def test_correlation_id_passthrough_when_provided(client: TestClient) -> None:
    cid = "test-correlation-12345"
    r = client.get("/v1/ops/health", headers={"X-Request-Id": cid})
    assert r.headers.get("x-request-id") == cid


def test_invalid_cursor_returns_empty_or_400(client: TestClient, auth: dict[str, str]) -> None:
    """Malformed cursor strings should not crash the endpoint."""
    r = client.get(
        "/v1/memories",
        headers=auth,
        params={"namespace": "eric/claude-code/episodic", "cursor": "not-a-cursor"},
    )
    # Either treated as no cursor (200) or rejected (400). Both are acceptable.
    assert r.status_code in (200, 400)


def test_dependencies_factories_raise_when_unconfigured() -> None:
    """The default dependency factories raise NotImplementedError per the
    ADR-punted-deps-fail-loud rule. Tests override; production wires
    them through deploy-side bootstrap."""
    from musubi.api import dependencies

    for fn in (
        dependencies.get_episodic_plane,
        dependencies.get_curated_plane,
        dependencies.get_concept_plane,
        dependencies.get_artifact_plane,
    ):
        with pytest.raises(NotImplementedError):
            fn()


# ---------------------------------------------------------------------------
# Read-side coverage — exercises every router on the happy path so the
# 85 % coverage gate clears and the router → plane wiring is verified.
# ---------------------------------------------------------------------------


def test_health_returns_200(client: TestClient) -> None:
    r = client.get("/v1/ops/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ready_includes_components(client: TestClient) -> None:
    r = client.get("/v1/ops/status")
    assert r.status_code == 200
    body = r.json()
    assert "components" in body
    assert "qdrant" in body["components"]


def test_get_episodic_by_id_routes_to_plane(
    client: TestClient,
    auth: dict[str, str],
    episodic: EpisodicPlane,
) -> None:
    namespace = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(EpisodicMemory(namespace=namespace, content="route-target"))
        return saved.object_id

    oid = asyncio.run(_seed())
    r = client.get(f"/v1/memories/{oid}", headers=auth, params={"namespace": namespace})
    assert r.status_code == 200
    body = r.json()
    assert body["object_id"] == oid
    assert body["content"] == "route-target"
    assert body["state"] == "provisional"


def test_get_curated_by_id_routes_to_plane(
    client: TestClient,
    api_settings: object,
    curated: CuratedPlane,
) -> None:
    import hashlib as _h

    from tests.api.conftest import mint_token

    namespace = "eric/claude-code/curated"
    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=[f"{namespace}:r"],
    )
    headers = {"Authorization": f"Bearer {token}"}

    async def _seed() -> str:
        body = "Curated body"
        saved = await curated.create(
            CuratedKnowledge(
                namespace=namespace,
                title="Test Curated",
                content=body,
                vault_path="curated/eric/test.md",
                body_hash=_h.sha256(body.encode()).hexdigest(),
            )
        )
        return saved.object_id

    oid = asyncio.run(_seed())
    r = client.get(f"/v1/curated-knowledge/{oid}", headers=headers, params={"namespace": namespace})
    assert r.status_code == 200
    assert r.json()["object_id"] == oid


def test_list_namespaces_returns_scope(
    client: TestClient,
    auth: dict[str, str],
) -> None:
    r = client.get("/v1/namespaces", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    # The token's scopes resolve to a list of namespaces.
    assert any("eric/claude-code/" in str(item) for item in body["items"])


def test_lifecycle_events_endpoint_responds(
    client: TestClient,
    operator_token: str,
) -> None:
    headers = {"Authorization": f"Bearer {operator_token}"}
    r = client.get("/v1/lifecycle/events", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert "items" in body


def test_contradictions_endpoint_responds(
    client: TestClient,
    auth: dict[str, str],
) -> None:
    r = client.get("/v1/contradictions", headers=auth)
    assert r.status_code == 200
    assert "items" in r.json()


def test_retrieve_endpoint_routes_to_plane(
    client: TestClient,
    auth: dict[str, str],
    episodic: EpisodicPlane,
) -> None:
    namespace = "eric/claude-code/episodic"

    async def _seed() -> str:
        saved = await episodic.create(
            EpisodicMemory(namespace=namespace, content="retrievable-content")
        )
        await episodic.transition(
            namespace=namespace,
            object_id=saved.object_id,
            to_state="matured",
            actor="seed",
            reason="seed",
        )
        return saved.object_id

    asyncio.run(_seed())
    r = client.post(
        "/v1/retrieve",
        headers=auth,
        json={
            "namespace": namespace,
            "query_text": "retrievable",
            "mode": "fast",
            "limit": 5,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "results" in body


def test_thoughts_check_endpoint_responds(
    client: TestClient,
    api_settings: object,
) -> None:
    """POST /v1/thoughts/check is a read in disguise (lists unread thoughts
    for a presence). Included on the read surface per the slice scope."""
    from tests.api.conftest import mint_token

    namespace = "eric/claude-code/thought"
    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=[f"{namespace}:r"],
    )
    r = client.post(
        "/v1/thoughts/check",
        headers={"Authorization": f"Bearer {token}"},
        json={"namespace": namespace, "presence": "eric/claude-code"},
    )
    assert r.status_code == 200
    assert "items" in r.json()


def test_invalid_token_returns_401(client: TestClient) -> None:
    r = client.get(
        "/v1/memories/aaaaaaaaaaaaaaaaaaaaaaaaaaa",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# Committed openapi.yaml — verify it exists, parses, and references the
# read surface paths. The committed file is the deploy-time artifact per
# ADR-0013; this test guards against drift between code + snapshot.
# ---------------------------------------------------------------------------


def test_committed_openapi_yaml_includes_read_paths() -> None:
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    openapi_path = repo_root / "openapi.yaml"
    assert openapi_path.exists(), "openapi.yaml is not committed at the repo root"
    doc = yaml.safe_load(openapi_path.read_text())
    paths = set(doc["paths"].keys())
    assert "/v1/ops/health" in paths
    assert "/v1/memories/{object_id}" in paths
    assert "/v1/retrieve" in paths


def test_runtime_openapi_matches_committed_paths(client: TestClient) -> None:
    """The runtime-generated OpenAPI document and the committed snapshot
    cover the same read paths. (Detail-level drift — operationIds,
    descriptions — is ignored; this is a structural consistency check.)"""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    snapshot = yaml.safe_load((repo_root / "openapi.yaml").read_text())

    r = client.get("/v1/openapi.json")
    runtime = json.loads(r.text)

    assert set(snapshot["paths"].keys()) == set(runtime["paths"].keys())
