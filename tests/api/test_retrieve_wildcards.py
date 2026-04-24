"""Test contract for slice-api-retrieve-wildcards.

Implements the bullets from
[[_slices/slice-api-retrieve-wildcards]] § Test Contract — wildcard `*`
segments in `POST /v1/retrieve` namespaces, per
[[13-decisions/0031-retrieve-wildcard-namespace|ADR 0031]].

Three layers:

- **Unit on `_resolve_targets`** — syntactic shape validation, no Qdrant.
- **Unit on `_expand_wildcard_targets`** — Qdrant-backed enumeration via
  the in-memory ``qdrant`` fixture from ``conftest``.
- **End-to-end** — HTTP through the FastAPI app, seeded planes, real
  scope checks.

Plus write-side rejection tests confirming the existing namespace regex
still keeps `*` out of stored namespaces (per ADR — wildcards are
read-only).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from musubi.api.routers.retrieve import (
    _expand_wildcard_targets,
    _resolve_targets,
)
from musubi.planes.episodic import EpisodicPlane
from musubi.types.episodic import EpisodicMemory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_episodic(plane: EpisodicPlane, namespace: str, content: str) -> None:
    """Seed one matured episodic row at ``namespace``."""

    async def _go() -> None:
        saved = await plane.create(EpisodicMemory(namespace=namespace, content=content))
        await plane.transition(
            namespace=namespace,
            object_id=saved.object_id,
            to_state="matured",
            actor="seed",
            reason="seed",
        )

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# 1–10  _resolve_targets — syntactic shape validation
# ---------------------------------------------------------------------------


def test_wildcard_in_tenant_segment_3seg_accepted() -> None:
    targets, err = _resolve_targets("*/voice/episodic", None)
    assert err is None
    assert targets == [("*/voice/episodic", "episodic")]


def test_wildcard_in_presence_segment_3seg_accepted() -> None:
    targets, err = _resolve_targets("nyla/*/episodic", None)
    assert err is None
    assert targets == [("nyla/*/episodic", "episodic")]


def test_wildcard_in_plane_segment_3seg_with_planes_list_accepted() -> None:
    targets, err = _resolve_targets("nyla/voice/*", ["episodic", "curated"])
    assert err is None
    assert targets == [
        ("nyla/voice/episodic", "episodic"),
        ("nyla/voice/curated", "curated"),
    ]


def test_wildcard_in_plane_segment_3seg_without_planes_list_400s() -> None:
    targets, err = _resolve_targets("nyla/voice/*", None)
    assert err is not None and "planes" in err.lower()
    assert targets == []


def test_double_segment_wildcard_3seg_accepted() -> None:
    targets, err = _resolve_targets("nyla/*/*", ["episodic"])
    assert err is None
    assert targets == [("nyla/*/episodic", "episodic")]


def test_all_wildcard_3seg_accepted() -> None:
    targets, err = _resolve_targets("*/*/*", ["episodic"])
    assert err is None
    assert targets == [("*/*/episodic", "episodic")]


def test_wildcard_in_2seg_accepted() -> None:
    """2-seg `nyla/*` defaults to planes=["episodic"] like its concrete cousin."""
    targets, err = _resolve_targets("nyla/*", None)
    assert err is None
    assert targets == [("nyla/*/episodic", "episodic")]


def test_double_star_rejected() -> None:
    targets, err = _resolve_targets("nyla/**/episodic", None)
    assert err is not None
    assert targets == []


def test_empty_segment_with_wildcard_rejected() -> None:
    targets, err = _resolve_targets("nyla//episodic", None)
    assert err is not None and "empty" in err.lower()
    assert targets == []


def test_pattern_with_4_segments_rejected() -> None:
    targets, err = _resolve_targets("a/b/c/d", None)
    assert err is not None
    assert targets == []


# ---------------------------------------------------------------------------
# 11–17  _expand_wildcard_targets — Qdrant-backed enumeration
# ---------------------------------------------------------------------------


def test_expansion_returns_concrete_namespaces_for_wildcard_pattern(
    qdrant: object, episodic: EpisodicPlane
) -> None:
    _seed_episodic(episodic, "nyla/voice/episodic", "voice memory")
    _seed_episodic(episodic, "nyla/openclaw/episodic", "openclaw memory")

    expanded = _expand_wildcard_targets(qdrant, [("nyla/*/episodic", "episodic")])

    namespaces = sorted(ns for ns, _ in expanded)
    assert namespaces == ["nyla/openclaw/episodic", "nyla/voice/episodic"]
    assert all(plane == "episodic" for _, plane in expanded)


def test_expansion_filters_by_segment_count(
    qdrant: object, episodic: EpisodicPlane
) -> None:
    """A 3-seg pattern can only match 3-seg stored namespaces. Stored
    namespaces are always 3-seg today (regex), so this asserts the pattern's
    own segment count is honoured: a hypothetical 2-seg pattern wouldn't
    match a 3-seg row even if the prefix matched."""
    _seed_episodic(episodic, "nyla/voice/episodic", "x")
    # Pattern is 2-seg so even though "nyla/voice" is a prefix of the
    # stored 3-seg namespace, it should not match.
    expanded = _expand_wildcard_targets(qdrant, [("nyla/*", "episodic")])
    # 2-seg pattern fed in here would be a programming error in the router
    # (router resolves 2-seg to 3-seg before calling expansion). The
    # expansion routine still respects segment count: empty result.
    namespaces = [ns for ns, _ in expanded]
    assert namespaces == []


def test_expansion_segment_match_is_literal_not_substring(
    qdrant: object, episodic: EpisodicPlane
) -> None:
    """`*` is a whole-segment wildcard, not a regex char. Pattern
    `n*/voice/episodic` must NOT match `nyla/voice/episodic` — the first
    segment has a literal `n` followed by `*`, which is a syntactically
    invalid pattern at the segment level (segments are either fully
    literal or exactly `*`)."""
    _seed_episodic(episodic, "nyla/voice/episodic", "x")
    # Mixed-segment patterns: in our model `n*` is not "starts with n",
    # it's "literal segment n*" — which doesn't match `nyla`. Empty result.
    expanded = _expand_wildcard_targets(qdrant, [("n*/voice/episodic", "episodic")])
    namespaces = [ns for ns, _ in expanded]
    assert namespaces == []


def test_expansion_dedups_namespaces(
    qdrant: object, episodic: EpisodicPlane
) -> None:
    _seed_episodic(episodic, "nyla/voice/episodic", "first")
    _seed_episodic(episodic, "nyla/voice/episodic", "second")
    _seed_episodic(episodic, "nyla/voice/episodic", "third")

    expanded = _expand_wildcard_targets(qdrant, [("nyla/*/episodic", "episodic")])

    namespaces = [ns for ns, _ in expanded]
    assert namespaces == ["nyla/voice/episodic"]


def test_expansion_returns_empty_list_when_no_match(qdrant: object) -> None:
    expanded = _expand_wildcard_targets(qdrant, [("nyla/*/episodic", "episodic")])
    assert expanded == []


def test_no_wildcard_passes_through_unchanged(
    qdrant: object, episodic: EpisodicPlane
) -> None:
    """A concrete (no-`*`) target is not scrolled — the helper short-circuits."""
    _seed_episodic(episodic, "nyla/voice/episodic", "x")
    expanded = _expand_wildcard_targets(qdrant, [("nyla/voice/episodic", "episodic")])
    assert expanded == [("nyla/voice/episodic", "episodic")]


def test_expansion_runs_per_plane_in_targets_list(
    qdrant: object, episodic: EpisodicPlane
) -> None:
    """A targets list that names episodic AND curated wildcards should
    enumerate each plane's collection independently. Curated has no rows
    here, so curated leg returns empty; episodic returns its match."""
    _seed_episodic(episodic, "nyla/voice/episodic", "x")
    expanded = _expand_wildcard_targets(
        qdrant,
        [
            ("nyla/*/episodic", "episodic"),
            ("nyla/*/curated", "curated"),
        ],
    )
    # Only the episodic match survives; curated is empty.
    assert expanded == [("nyla/voice/episodic", "episodic")]


# ---------------------------------------------------------------------------
# 18–20  End-to-end retrieve through HTTP
# ---------------------------------------------------------------------------


def test_retrieve_with_wildcard_returns_results_from_multiple_channels(
    client: TestClient, episodic: EpisodicPlane, api_settings: object
) -> None:
    """The headline behaviour: write to two channels, retrieve with a
    presence wildcard, get hits from both."""
    from tests.api.conftest import mint_token

    _seed_episodic(episodic, "nyla/voice/episodic", "Eric mentioned the dentist on the call")
    _seed_episodic(episodic, "nyla/openclaw/episodic", "Dentist appointment Tuesday afternoon")

    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["nyla/*/*:r"],
        presence="nyla/voice",
    )
    r = client.post(
        "/v1/retrieve",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "namespace": "nyla/*/episodic",
            "query_text": "dentist",
            "mode": "fast",
            "limit": 10,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    namespaces = {row["namespace"] for row in body["results"]}
    assert "nyla/voice/episodic" in namespaces, namespaces
    assert "nyla/openclaw/episodic" in namespaces, namespaces


def test_retrieve_with_wildcard_no_matches_returns_empty_results_not_404(
    client: TestClient, api_settings: object
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["nyla/*/*:r"],
        presence="nyla/voice",
    )
    r = client.post(
        "/v1/retrieve",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "namespace": "nyla/*/episodic",
            "query_text": "anything",
            "mode": "fast",
            "limit": 5,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["results"] == []


def test_retrieve_with_wildcard_response_rows_carry_origin_namespace(
    client: TestClient, episodic: EpisodicPlane, api_settings: object
) -> None:
    """Every row's `namespace` is the concrete 3-seg slot it lives in,
    never the wildcard pattern. The pattern is a query primitive; the
    row keeps its provenance."""
    from tests.api.conftest import mint_token

    _seed_episodic(episodic, "nyla/voice/episodic", "voice content")
    _seed_episodic(episodic, "nyla/openclaw/episodic", "openclaw content")

    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["nyla/*/*:r"],
        presence="nyla/voice",
    )
    r = client.post(
        "/v1/retrieve",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "namespace": "nyla/*/episodic",
            "query_text": "content",
            "mode": "fast",
            "limit": 10,
        },
    )
    assert r.status_code == 200, r.text
    for row in r.json()["results"]:
        assert "*" not in row["namespace"], row
        # 3-seg, not the pattern
        assert row["namespace"].count("/") == 2, row


# ---------------------------------------------------------------------------
# 21–23  Strict scope on the expanded list
# ---------------------------------------------------------------------------


def test_retrieve_wildcard_403_when_token_lacks_read_on_one_expansion_target(
    client: TestClient, episodic: EpisodicPlane, api_settings: object
) -> None:
    """A token scoped only to nyla/voice cannot wildcard across nyla's
    channels — `nyla/openclaw/episodic` is in the expansion and the
    strict per-target scope check (ADR 0028) trips."""
    from tests.api.conftest import mint_token

    _seed_episodic(episodic, "nyla/voice/episodic", "voice")
    _seed_episodic(episodic, "nyla/openclaw/episodic", "openclaw")

    voice_only = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["nyla/voice/episodic:r"],
        presence="nyla/voice",
    )
    r = client.post(
        "/v1/retrieve",
        headers={"Authorization": f"Bearer {voice_only}"},
        json={
            "namespace": "nyla/*/episodic",
            "query_text": "anything",
            "mode": "fast",
            "limit": 5,
        },
    )
    assert r.status_code == 403, r.text


def test_retrieve_wildcard_200_when_token_has_wildcard_read_scope(
    client: TestClient, episodic: EpisodicPlane, api_settings: object
) -> None:
    from tests.api.conftest import mint_token

    _seed_episodic(episodic, "nyla/voice/episodic", "voice")
    _seed_episodic(episodic, "nyla/openclaw/episodic", "openclaw")

    wildcard = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["nyla/*/*:r"],
        presence="nyla/voice",
    )
    r = client.post(
        "/v1/retrieve",
        headers={"Authorization": f"Bearer {wildcard}"},
        json={
            "namespace": "nyla/*/episodic",
            "query_text": "voice",
            "mode": "fast",
            "limit": 5,
        },
    )
    assert r.status_code == 200, r.text


def test_retrieve_wildcard_first_403_aborts_no_partial_results(
    client: TestClient, episodic: EpisodicPlane, api_settings: object
) -> None:
    """Confirms ADR 0028 strictness still holds under wildcards — partial
    results are silent failure mode and we explicitly reject them."""
    from tests.api.conftest import mint_token

    _seed_episodic(episodic, "nyla/voice/episodic", "voice")
    _seed_episodic(episodic, "nyla/openclaw/episodic", "openclaw")

    partial = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["nyla/voice/episodic:r"],
        presence="nyla/voice",
    )
    r = client.post(
        "/v1/retrieve",
        headers={"Authorization": f"Bearer {partial}"},
        json={
            "namespace": "nyla/*/episodic",
            "query_text": "voice",
            "mode": "fast",
            "limit": 5,
        },
    )
    assert r.status_code == 403, r.text
    # Body has no `results` key — full reject, not a partial.
    assert "results" not in r.json()


# ---------------------------------------------------------------------------
# 24–25  Write-side rejection (locks the ADR's read-only-wildcard rule)
# ---------------------------------------------------------------------------


def test_episodic_send_with_wildcard_namespace_400s(
    client: TestClient, auth: dict[str, str]
) -> None:
    """`*` is not in the namespace regex character class, so writes 400
    on validation. This test locks that behaviour against future regex
    drift."""
    r = client.post(
        "/v1/episodic/send",
        headers=auth,
        json={
            "namespace": "nyla/*/episodic",
            "content": "should not land",
        },
    )
    assert r.status_code in (400, 422), r.text


def test_thoughts_send_with_wildcard_namespace_400s(
    client: TestClient, api_settings: object
) -> None:
    from tests.api.conftest import mint_token

    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["nyla/*/*:rw"],
        presence="nyla/voice",
    )
    r = client.post(
        "/v1/thoughts/send",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "namespace": "nyla/*/thought",
            "from_presence": "nyla/voice",
            "to_presence": "all",
            "content": "should not land",
        },
    )
    assert r.status_code in (400, 422), r.text


# ---------------------------------------------------------------------------
# 26–27  SDK ergonomics — `planes` parameter passes through
# ---------------------------------------------------------------------------


def test_sdk_async_retrieve_passes_planes_through() -> None:
    import httpx

    from musubi.sdk.async_client import AsyncMusubiClient

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(
            200, json={"results": [], "mode": "fast", "limit": 10}
        )

    async def _go() -> None:
        transport = httpx.MockTransport(handler)
        async with AsyncMusubiClient(
            base_url="http://test",
            token="t",
            transport=transport,
        ) as c:
            await c.retrieve(
                namespace="nyla/*/episodic",
                query_text="x",
                planes=["episodic"],
            )

    asyncio.run(_go())
    body = captured["body"]
    assert isinstance(body, str)
    assert '"planes"' in body and '"episodic"' in body


def test_sdk_sync_retrieve_passes_planes_through() -> None:
    import httpx

    from musubi.sdk.client import MusubiClient

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(
            200, json={"results": [], "mode": "fast", "limit": 10}
        )

    transport = httpx.MockTransport(handler)
    with MusubiClient(
        base_url="http://test",
        token="t",
        transport=transport,
    ) as c:
        c.retrieve(
            namespace="nyla/*/episodic",
            query_text="x",
            planes=["episodic"],
        )

    body = captured["body"]
    assert isinstance(body, str)
    assert '"planes"' in body and '"episodic"' in body


# ---------------------------------------------------------------------------
# 28–29  Hypothesis / property
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "concrete_ns",
    [
        "nyla/voice/episodic",
        "nyla/openclaw/curated",
        "aoi/voice/concept",
        "system/lifecycle-worker/lifecycle",
    ],
)
def test_concrete_3seg_namespace_passes_through_expansion_unchanged(
    concrete_ns: str, qdrant: object
) -> None:
    """Property: any non-wildcard 3-seg namespace is idempotent under expansion."""
    plane = concrete_ns.rsplit("/", 1)[-1]
    expanded = _expand_wildcard_targets(qdrant, [(concrete_ns, plane)])
    assert expanded == [(concrete_ns, plane)]


def test_every_result_namespace_satisfies_the_pattern(
    client: TestClient, episodic: EpisodicPlane, api_settings: object
) -> None:
    """Property: every row in the response has a `namespace` that
    segment-matches the requested wildcard pattern. A `*` segment
    matches anything; a literal segment must equal the row's segment."""
    from tests.api.conftest import mint_token

    _seed_episodic(episodic, "nyla/voice/episodic", "alpha")
    _seed_episodic(episodic, "nyla/openclaw/episodic", "beta")

    token = mint_token(
        api_settings,  # type: ignore[arg-type]
        scopes=["nyla/*/*:r"],
        presence="nyla/voice",
    )
    pattern = "nyla/*/episodic"
    pattern_segs = pattern.split("/")
    r = client.post(
        "/v1/retrieve",
        headers={"Authorization": f"Bearer {token}"},
        json={"namespace": pattern, "query_text": "alpha", "mode": "fast", "limit": 5},
    )
    assert r.status_code == 200, r.text
    for row in r.json()["results"]:
        row_segs = row["namespace"].split("/")
        assert len(row_segs) == len(pattern_segs)
        for ps, rs in zip(pattern_segs, row_segs, strict=True):
            assert ps == "*" or ps == rs, (pattern, row["namespace"])
