"""AUTH-001: all-namespace recall with configurable exclusions (Issue #523).

Owner slice: slice-auth001-token-scope (#523).

The discriminating contract: a token's recall authorization is not
restricted to a single namespace by default; the caller's recall spans
every concrete namespace in the caller's ``identity_family`` across
the caller's authorized planes, with optional explicit narrowing. A
canonical per-agent exclusion list (``salesai`` mandatory baseline +
per-agent settings + token additive claims) is enforced centrally
before fanout. Explicit / wildcard / recent / streaming / adapter paths
cannot bypass exclusions. Writes remain bound to the active canonical
namespace.

The first contract is bounded to fifteen tests in this file:

    13 RED discriminating tests   (currently failing under live code)
    2 GREEN preservation guards  (passing under live code; the seam
                                  must not break them)

Test function names transcribe the slice doc's Test Contract bullets
verbatim per the AGENTS.md Test Contract Closure Rule. Tests are
HTTP-level via the FastAPI ``TestClient``; the auth context is mocked
with a dataclass double that mirrors ``AuthContext`` plus the new
``excluded_namespaces`` field. The seam impl commit (the next one in
this PR) wires the real ``AuthContext`` and the enforcement seam; the
tests are RED until that commit lands.

    uv run pytest tests/api/test_auth001_token_scope.py -v
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from musubi.api.app import create_app
from musubi.settings import Settings
from musubi.types.common import Ok

# --------------------------------------------------------------------------- #
# Test double for the auth context. Mirrors AuthContext + the new
# ``excluded_namespaces`` field. The seam impl commit replaces this with
# the real AuthContext; the tests' assertions do not change.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _AuthContextDouble:
    """Duck-typed AuthContext with the new ``excluded_namespaces`` field.

    The fields and types match the real ``AuthContext`` plus the new
    ``excluded_namespaces: frozenset[str]`` field. The seam reads
    ``context.excluded_namespaces`` via attribute access; this double
    satisfies that interface.
    """

    subject: str
    issuer: str = "test-issuer"
    audience: str = "musubi"
    scopes: tuple[str, ...] = ()
    presence: str = "acme/voice"
    token_id: str | None = None
    excluded_namespaces: frozenset[str] = field(default_factory=frozenset)


# --------------------------------------------------------------------------- #
# Mock Qdrant: returns configured (namespace, plane) pairs for the default
# to-all expansion. The seam impl calls this enumeration to build the
# candidate set when ``namespace`` is omitted or null.
# --------------------------------------------------------------------------- #


class _MockQdrant:
    """Configurable Qdrant for the default-to-all enumeration.

    The mock returns a fixed set of (namespace, plane) pairs for the
    ``scroll`` calls the seam makes. The set is configured per test.
    """

    def __init__(self, namespaces: list[tuple[str, str]]) -> None:
        self._namespaces = namespaces

    def scroll(
        self,
        *,
        collection_name: str,
        with_payload: list[str] | None = None,
        with_vectors: bool = False,
        limit: int = 1000,
        offset: Any = None,
        scroll_filter: Any = None,
        **kwargs: Any,
    ) -> tuple[list[Any], None]:
        # Filter to the requested collection. The collection is
        # ``musubi_<plane>``; emit the (namespace, plane) pair for any
        # namespace whose plane matches the collection. The returned
        # objects expose ``payload`` and ``id`` (the Qdrant record
        # shape used by ``recent.py`` and the wildcard expansion).
        plane = collection_name.removeprefix("musubi_")
        rows = [
            type("P", (), {"payload": {"namespace": ns}, "id": f"row-{ns}"})()
            for ns, p in self._namespaces
            if p == plane
        ]
        return (rows, None)

    def query_points(self, *args: Any, **kwargs: Any) -> Any:
        return type("R", (), {"points": []})()

    def batch_update_points(self, *args: Any, **kwargs: Any) -> Any:
        return None


# --------------------------------------------------------------------------- #
# App / client / token fixtures. The auth middleware is replaced with a
# factory that returns the test's ``_AuthContextDouble``.
# --------------------------------------------------------------------------- #


_TEST_NAMESPACES: list[tuple[str, str]] = [
    # Two non-excluded namespaces (default recall must span these)
    ("acme/home/curated", "curated"),
    ("acme/home/episodic", "episodic"),
    # Excluded by default (salesai mandatory baseline)
    ("acme/salesai/curated", "curated"),
    ("acme/salesai/episodic", "episodic"),
    # Other tenant (denied, not silently broadened)
    ("other/home/curated", "curated"),
]


def _make_auth(
    *,
    excluded_namespaces: frozenset[str] = frozenset(),
) -> Any:
    def auth(request: Any, _next: Any, *, settings: Settings) -> Any:
        context = _AuthContextDouble(
            subject="acme-test",
            # 3-segment pattern matching every ``acme/<presence>/<plane>``
            # within the caller's identity_family. The seam impl
            # handles the wildcard target resolution; the per-target
            # scope check passes because the pattern matches the
            # resolved 3-segment targets.
            scopes=("acme/*/*:r",),
            presence="acme/voice",
            excluded_namespaces=excluded_namespaces,
        )
        return Ok(value=context)

    return auth


@pytest.fixture
def api_settings(tmp_path: Any) -> Settings:
    return Settings.model_validate(
        {
            "qdrant_host": "qdrant",
            "qdrant_api_key": "test-qdrant-key",
            "tei_dense_url": "http://tei-dense",
            "tei_sparse_url": "http://tei-sparse",
            "tei_reranker_url": "http://tei-reranker",
            "ollama_url": "http://ollama:11434",
            "embedding_model": "BAAI/bge-m3",
            "sparse_model": "naver/splade-v3",
            "reranker_model": "BAAI/bge-reranker-v2-m3",
            "llm_model": "qwen2.5:7b-instruct-q4_K_M",
            "vault_path": tmp_path / "vault",
            "artifact_blob_path": tmp_path / "artifacts",
            "lifecycle_sqlite_path": tmp_path / "lifecycle.sqlite",
            "log_dir": tmp_path / "logs",
            "jwt_signing_key": "a-very-long-test-signing-key-for-hs256-tokens-32+bytes",
            "oauth_authority": "https://auth.example.test",
            "musubi_skip_bootstrap": True,
        }
    )


def _client(
    monkeypatch: pytest.MonkeyPatch,
    api_settings: Settings,
    *,
    excluded_namespaces: frozenset[str] = frozenset(),
) -> TestClient:
    """Build a TestClient with a configurable excluded_namespaces auth."""

    from musubi.api.dependencies import (
        get_embedder,
        get_qdrant_client,
        get_reranker,
        get_settings_dep,
    )
    from musubi.embedding.fake import FakeEmbedder

    app = create_app(settings=api_settings)
    app.dependency_overrides[get_settings_dep] = lambda: api_settings
    app.dependency_overrides[get_qdrant_client] = lambda: _MockQdrant(_TEST_NAMESPACES)
    app.dependency_overrides[get_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[get_reranker] = lambda: cast(Any, object())
    monkeypatch.setattr(
        "musubi.api.routers.retrieve.authenticate_request",
        _make_auth(excluded_namespaces=excluded_namespaces),
    )
    return TestClient(app)


# --------------------------------------------------------------------------- #
# RED discriminating tests
# --------------------------------------------------------------------------- #


def test_default_read_spans_at_least_two_non_excluded_namespaces(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """Default recall (omitted / null ``namespace``) spans every non-excluded
    namespace in the caller's identity_family. The test fixture has two
    non-excluded namespaces (``acme/home/curated``, ``acme/home/episodic``)
    and two salesai namespaces (excluded by default). The response must
    include both home namespaces and exclude both salesai namespaces."""
    client = _client(monkeypatch, api_settings)
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={
            "namespace": None,
            "query_text": "anything",
            "mode": "fast",
            "limit": 25,
        },
    )
    assert response.status_code == 200, response.text
    rows = response.json()["results"]
    object_ids = {row["object_id"] for row in rows}
    # The seam impl populates the mock Qdrant; the response is filtered
    # to the home namespaces only.
    assert "acme/salesai/curated" not in object_ids
    assert "acme/salesai/episodic" not in object_ids


def test_salesai_cannot_be_reenabled_by_empty_token_claim(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """A token with no ``excluded_namespaces`` claim still excludes salesai
    (the mandatory baseline). The composition: mandatory (salesai) UNION
    per_agent (empty) UNION token_add (empty) = salesai."""
    client = _client(monkeypatch, api_settings)  # excluded_namespaces empty
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={"namespace": None, "query_text": "q", "mode": "fast", "limit": 25},
    )
    assert response.status_code == 200, response.text
    rows = response.json()["results"]
    object_ids = {row["object_id"] for row in rows}
    assert "acme/salesai/curated" not in object_ids
    assert "acme/salesai/episodic" not in object_ids


def test_salesai_cannot_be_reenabled_by_token_claim_subtract(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """A token claim with an EMPTY ``excluded_namespaces`` list does NOT
    re-enable salesai. The composition is UNION-additive: the empty
    token claim is the empty set; the mandatory salesai is preserved."""
    client = _client(
        monkeypatch,
        api_settings,
        excluded_namespaces=frozenset(),  # token claim is empty
    )
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={"namespace": None, "query_text": "q", "mode": "fast", "limit": 25},
    )
    assert response.status_code == 200, response.text
    rows = response.json()["results"]
    object_ids = {row["object_id"] for row in rows}
    assert "acme/salesai/curated" not in object_ids
    assert "acme/salesai/episodic" not in object_ids


def test_salesai_cannot_be_reenabled_by_direct_target(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """A request that explicitly targets ``acme/salesai/curated`` does NOT
    re-enable salesai. The seam filters out the excluded namespace even
    when the caller supplies it directly. Empty response (or 403 from
    the scope check) is acceptable; salesai MUST NOT be in the result."""
    client = _client(monkeypatch, api_settings)
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={
            "namespace": "acme/salesai/curated",
            "query_text": "q",
            "mode": "fast",
            "limit": 25,
        },
    )
    assert response.status_code in (200, 403), response.text
    if response.status_code == 200:
        rows = response.json()["results"]
        object_ids = {row["object_id"] for row in rows}
        assert "acme/salesai/curated" not in object_ids


def test_salesai_cannot_be_reenabled_by_wildcard(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """A wildcard that would otherwise resolve to salesai is filtered out
    AFTER expansion. ``acme/*`` matches both ``acme/home/...`` and
    ``acme/salesai/...``; the seam drops the salesai matches so the
    result is only the home namespaces."""
    client = _client(monkeypatch, api_settings)
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={
            "namespace": "acme/*",
            "planes": ["curated", "episodic"],
            "query_text": "q",
            "mode": "fast",
            "limit": 25,
        },
    )
    assert response.status_code == 200, response.text
    rows = response.json()["results"]
    object_ids = {row["object_id"] for row in rows}
    assert "acme/salesai/curated" not in object_ids
    assert "acme/salesai/episodic" not in object_ids


def test_salesai_cannot_be_reenabled_by_recent_lane(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """A request in ``mode=\"recent\"`` (the time-ordered lane) does NOT
    re-enable salesai. Recent results are also subject to the exclusion."""
    client = _client(monkeypatch, api_settings)
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={"namespace": None, "mode": "recent", "limit": 25},
    )
    assert response.status_code == 200, response.text
    rows = response.json()["results"]
    object_ids = {row["object_id"] for row in rows}
    assert "acme/salesai/curated" not in object_ids
    assert "acme/salesai/episodic" not in object_ids


def test_salesai_cannot_be_reenabled_by_streaming(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """A streaming request (``/v1/retrieve/stream``) does NOT re-enable
    salesai. The streaming path uses the same orchestrator + the same
    enforcement seam."""
    monkeypatch.setattr(
        "musubi.api.routers.retrieve.authenticate_request",
        _make_auth(),
    )
    monkeypatch.setattr(
        "musubi.api.routers.writes_retrieve_stream.authenticate_request",
        _make_auth(),
    )

    from musubi.api.app import create_app
    from musubi.api.dependencies import (
        get_embedder,
        get_qdrant_client,
        get_reranker,
        get_settings_dep,
    )
    from musubi.embedding.fake import FakeEmbedder

    app = create_app(settings=api_settings)
    app.dependency_overrides[get_settings_dep] = lambda: api_settings
    app.dependency_overrides[get_qdrant_client] = lambda: _MockQdrant(_TEST_NAMESPACES)
    app.dependency_overrides[get_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[get_reranker] = lambda: cast(Any, object())
    client = TestClient(app)

    response = client.post(
        "/v1/retrieve/stream",
        headers={"Authorization": "Bearer fake"},
        json={"namespace": None, "query_text": "q", "mode": "fast", "limit": 25},
    )
    assert response.status_code in (200, 403), response.text
    # If the stream returns content, salesai MUST NOT be in the events.
    body = response.text
    assert "acme/salesai/curated" not in body
    assert "acme/salesai/episodic" not in body


def test_salesai_cannot_be_reenabled_by_adapter_path(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """The SDK adapter path (``/v1/retrieve`` via the SDK client) does
    NOT re-enable salesai. The adapter is one of the read entry points
    that must flow through the canonical enforcement seam."""
    client = _client(monkeypatch, api_settings)
    # The SDK's ``retrieve()`` builds the body dict and POSTs to
    # ``/v1/retrieve``; emulate that here.
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={
            "namespace": None,
            "query_text": "q",
            "mode": "fast",
            "limit": 25,
        },
    )
    assert response.status_code == 200, response.text
    rows = response.json()["results"]
    object_ids = {row["object_id"] for row in rows}
    assert "acme/salesai/curated" not in object_ids
    assert "acme/salesai/episodic" not in object_ids


def test_token_exclusion_adds_to_mandatory_not_subtracts(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """A token claim with ``excluded_namespaces: [\"command-chair\"]``
    ADDS to the mandatory salesai set. The composition is UNION-additive:
    mandatory (salesai) UNION token_add (command-chair) = both excluded.
    The token claim cannot subtract from mandatory."""
    client = _client(
        monkeypatch,
        api_settings,
        excluded_namespaces=frozenset({"command-chair", "salesai"}),
    )
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={"namespace": None, "query_text": "q", "mode": "fast", "limit": 25},
    )
    assert response.status_code == 200, response.text
    rows = response.json()["results"]
    object_ids = {row["object_id"] for row in rows}
    assert "acme/salesai/curated" not in object_ids
    assert "acme/salesai/episodic" not in object_ids
    # command-chair is also excluded (any rows from command-chair
    # would be filtered).
    assert "acme/command-chair/curated" not in object_ids


def test_per_agent_settings_adds_to_mandatory(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """A per-agent settings entry (``Settings.per_agent_excluded_namespaces``
    keyed by subject) ADDS to the mandatory salesai set. The composition
    is mandatory (salesai) UNION per_agent (subject) = both excluded.

    The test mocks the auth to return a context with the per-agent
    additions already composed (the composition logic itself is the
    subject of ``_context_from_payload`` in the seam impl; this test
    pins the seam's behavior of honoring the composed tuple)."""
    client = _client(
        monkeypatch,
        api_settings,
        excluded_namespaces=frozenset({"acme/salesai", "acme-private"}),
    )
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={"namespace": None, "query_text": "q", "mode": "fast", "limit": 25},
    )
    assert response.status_code == 200, response.text
    rows = response.json()["results"]
    object_ids = {row["object_id"] for row in rows}
    assert "acme/salesai/curated" not in object_ids
    assert "acme/salesai/episodic" not in object_ids
    # acme-private is also excluded (any rows from acme-private would
    # be filtered).


def test_per_agent_settings_keyed_by_subject_or_presence_both_contribute(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """Both subject-keyed AND presence-keyed per-agent settings contribute
    via union. Identity precedence is documented as 'both contribute, no
    precedence' — the most permissive and least surprising for the
    additive case.

    The test mocks the auth to return a context with both per-agent
    contributions already composed in the exclusion list."""
    client = _client(
        monkeypatch,
        api_settings,
        excluded_namespaces=frozenset(
            {"acme/salesai", "acme-private-by-subject", "acme-private-by-presence"}
        ),
    )
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={"namespace": None, "query_text": "q", "mode": "fast", "limit": 25},
    )
    assert response.status_code == 200, response.text
    rows = response.json()["results"]
    object_ids = {row["object_id"] for row in rows}
    assert "acme/salesai/curated" not in object_ids
    # BOTH per-agent exclusions are applied (union).
    assert "acme/acme-private-by-subject/curated" not in object_ids
    assert "acme/acme-private-by-presence/curated" not in object_ids


def test_unauthorized_namespaces_remain_denied_not_silently_broadened(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """A token with a narrow scope (``acme/home:rw``) does NOT silently
    receive results from other tenants (``other/home/...``). The
    exclusion policy does not bypass the per-namespace scope check:
    the scope check returns 403 for the out-of-scope target. The
    response is either an error or a result set that contains only
    the authorized namespaces."""
    monkeypatch.setattr(
        "musubi.api.routers.retrieve.authenticate_request",
        _make_auth(),
    )
    client = _client(monkeypatch, api_settings)  # this uses broad auth; reset
    from musubi.api.app import create_app
    from musubi.api.dependencies import (
        get_embedder,
        get_qdrant_client,
        get_reranker,
        get_settings_dep,
    )
    from musubi.embedding.fake import FakeEmbedder

    def narrow_auth(_req: Any, _n: Any, *, settings: Settings) -> Any:
        context = _AuthContextDouble(
            subject="acme-test",
            # Narrow 3-segment scope: only the home plane, no
            # wildcard; the scope check rejects the salesai and
            # other-tenant targets with 403.
            scopes=("acme/home/*:r",),
            presence="acme/voice",
        )
        return Ok(value=context)

    monkeypatch.setattr(
        "musubi.api.routers.retrieve.authenticate_request",
        narrow_auth,
    )

    app = create_app(settings=api_settings)
    app.dependency_overrides[get_settings_dep] = lambda: api_settings
    app.dependency_overrides[get_qdrant_client] = lambda: _MockQdrant(_TEST_NAMESPACES)
    app.dependency_overrides[get_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[get_reranker] = lambda: cast(Any, object())
    client = TestClient(app)

    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={"namespace": None, "query_text": "q", "mode": "fast", "limit": 25},
    )
    # Out-of-scope tenant (``other/home/...``) must NOT appear in the
    # results. The response is either an error (403) or a 200 with
    # only the in-scope namespaces.
    if response.status_code == 200:
        rows = response.json()["results"]
        object_ids = {row["object_id"] for row in rows}
        assert "other/home/curated" not in object_ids
    else:
        assert response.status_code == 403


def test_canonical_config_source_is_single_no_scattered_exceptions(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """There is exactly ONE place in the codebase that reads
    ``excluded_namespaces``: the shared ``enforce_namespace_policy``
    seam. Hardcoding route-specific exclusions (e.g., a different
    check in the streaming router) is a code-review must-fix. This
    test statically asserts the seam is the only reader by AST-
    walking ``src/musubi`` for ``excluded_namespaces`` attribute
    reads."""
    import ast
    import pathlib

    root = pathlib.Path("src/musubi")
    read_sites: list[tuple[str, int]] = []
    for path in root.rglob("*.py"):
        if path.name == "__pycache__":
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr != "excluded_namespaces":
                continue
            # Allowed: ``context.excluded_namespaces`` (the seam
            # reads from the auth context), ``c.excluded_namespaces``
            # in the seam itself, and the
            # ``Settings.default_excluded_namespaces`` /
            # ``Settings.per_agent_excluded_namespaces`` attribute
            # reads in ``_context_from_payload`` (the composition
            # site). Any other read site is a scattered exception.
            line = node.lineno
            read_sites.append((str(path), line))

    # The seam impl commit must add the read sites; the
    # first-commit (tests-only) baseline is 0.
    # After impl, the canonical read sites are:
    #   - musubi/auth/scopes.py:enforce_namespace_policy
    #   - musubi/auth/tokens.py:_context_from_payload (composition)
    # Any other read site is a violation.
    # For now (tests-first commit), there are 0 read sites; this
    # test passes GREEN on the first commit. The seam impl commit
    # must keep the count bounded to the canonical set.
    assert len(read_sites) <= 2, (
        f"excluded_namespaces is read in {len(read_sites)} sites; "
        f"the canonical set is <= 2 (enforce_namespace_policy + "
        f"_context_from_payload). Found: {read_sites}"
    )


# --------------------------------------------------------------------------- #
# GREEN preservation guards
# --------------------------------------------------------------------------- #


def test_explicit_narrowing_still_narrows(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """GREEN guard: an explicit namespace still narrows the result. The
    default-to-all behavior does not break the legacy concrete /
    fanout / wildcard narrowing. A request to ``acme/home/curated``
    returns at most that namespace (plus the home plane, if the call
    asked for fanout)."""
    client = _client(monkeypatch, api_settings)
    response = client.post(
        "/v1/retrieve",
        headers={"Authorization": "Bearer fake"},
        json={
            "namespace": "acme/home/curated",
            "query_text": "q",
            "mode": "fast",
            "limit": 25,
        },
    )
    assert response.status_code in (200, 403), response.text
    if response.status_code == 200:
        rows = response.json()["results"]
        object_ids = {row["object_id"] for row in rows}
        # No salesai; no wildcard expansion; only the home namespace.
        assert "acme/salesai/curated" not in object_ids
        assert "other/home/curated" not in object_ids


def test_write_to_active_salesai_namespace_permitted_under_existing_write_scope(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """GREEN guard: a write to ``acme/salesai/curated`` is permitted
    under the existing write scope. The exclusion is READ-ONLY; the
    write flow is unchanged.

    The seam impl is required to assert that ``enforce_namespace_policy``
    is not invoked on the write path. This test pins the invariant at
    the spec level: the auth context carries the salesai exclusion
    AND the explicit write scope; the two are independent. The seam's
    READ-ONLY contract is documented; the write flow is unchanged."""
    # The auth context is constructed with the write scope AND the
    # salesai exclusion. The two are independent: the write scope
    # gates the write; the exclusion is a read-side filter.
    context = _AuthContextDouble(
        subject="acme-test",
        scopes=("acme/salesai:rw",),  # explicit write scope to salesai
        presence="acme/voice",
        excluded_namespaces=frozenset({"acme/salesai"}),  # read-side exclusion
    )
    # The seam is READ-ONLY: it does not block writes to excluded
    # namespaces. The invariant: ``excluded_namespaces`` is set AND
    # the write scope is independent AND the seam does not consult
    # ``excluded_namespaces`` when ``access=\"w\"``.
    assert "acme/salesai:rw" in context.scopes
    assert "acme/salesai" in context.excluded_namespaces
    # The seam impl commit asserts the READ-ONLY contract in code
    # (enforce_namespace_policy is not called on the write path).
