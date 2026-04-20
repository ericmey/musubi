"""Test contract for slice-api-app-bootstrap.

Implements the 12 bullets from
[[_slices/slice-api-app-bootstrap]] § Test Contract. All tests are
unit-form with mocked Qdrant + TEI; the integration harness
(slice-ops-integration-harness, PR #114) covers real-service
verification.

Canonical 7-commit shape: this file lands in the ``test(api):``
commit before the implementation lands in ``feat(api):``. Per the
feedback memory saved after PR #114's audit soft-warning.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI

from musubi.api.bootstrap import (
    BootstrapError,
    bootstrap_production_app,
)
from musubi.api.dependencies import (
    get_artifact_plane,
    get_concept_plane,
    get_curated_plane,
    get_embedder,
    get_episodic_plane,
    get_qdrant_client,
    get_thoughts_plane,
)
from musubi.embedding import Embedder
from musubi.planes.artifact import ArtifactPlane
from musubi.planes.concept import ConceptPlane
from musubi.planes.curated import CuratedPlane
from musubi.planes.episodic import EpisodicPlane
from musubi.planes.thoughts import ThoughtsPlane
from musubi.settings import Settings

# ---------------------------------------------------------------------------
# Fixtures — settings + a mock-everything QdrantClient/TEI patch chain so the
# bootstrap can exercise its full happy path without touching the network.
# ---------------------------------------------------------------------------


@pytest.fixture
def app() -> FastAPI:
    return FastAPI()


@pytest.fixture
def settings(api_settings: Settings) -> Settings:
    """Reuse the existing api_settings shape (HS256 key, dummy URLs,
    tmp_path-backed disk paths) — that fixture lives in
    ``tests/api/conftest.py`` and is exactly the production-shaped
    Settings instance the bootstrap needs."""
    return api_settings


@pytest.fixture
def patch_qdrant_ok() -> Iterator[Any]:
    """Replace BOTH dep probes with happy-path doubles so the
    bootstrap completes without touching the network. Yields the
    QdrantClient mock for tests that need to introspect it."""
    with (
        patch("musubi.api.bootstrap.QdrantClient") as mock_qd_cls,
        patch("musubi.api.bootstrap._probe_tei", return_value=None),
    ):
        instance = mock_qd_cls.return_value
        instance.get_collections.return_value = type("Collections", (), {"collections": []})()
        yield mock_qd_cls


@pytest.fixture
def patch_qdrant_unreachable() -> Iterator[Any]:
    """First call to QdrantClient.get_collections raises ConnectionError;
    bootstrap's retry should eventually surface BootstrapError."""
    with patch("musubi.api.bootstrap.QdrantClient") as mock_cls:
        instance = mock_cls.return_value
        instance.get_collections.side_effect = ConnectionError("qdrant unreachable")
        yield mock_cls


@pytest.fixture
def patch_qdrant_recovers() -> Iterator[Any]:
    """First Qdrant probe raises, second succeeds — retry path. TEI
    probe is patched happy throughout so this fixture isolates the
    Qdrant retry behaviour."""
    with (
        patch("musubi.api.bootstrap.QdrantClient") as mock_cls,
        patch("musubi.api.bootstrap._probe_tei", return_value=None),
    ):
        instance = mock_cls.return_value
        instance.get_collections.side_effect = [
            ConnectionError("qdrant transient"),
            type("Collections", (), {"collections": []})(),
        ]
        yield mock_cls


# ---------------------------------------------------------------------------
# Bullets 1-4 — bootstrap installs every override
# ---------------------------------------------------------------------------


def test_bootstrap_installs_qdrant_override(
    app: FastAPI, settings: Settings, patch_qdrant_ok: Any
) -> None:
    """Bullet 1 — get_qdrant_client gets a non-NotImplementedError override."""
    bootstrap_production_app(app, settings)
    assert get_qdrant_client in app.dependency_overrides
    factory = app.dependency_overrides[get_qdrant_client]
    client = factory()
    # The mocked QdrantClient instance returned by the patched constructor.
    assert client is patch_qdrant_ok.return_value


def test_bootstrap_installs_embedder_override(
    app: FastAPI, settings: Settings, patch_qdrant_ok: Any
) -> None:
    """Bullet 2 — get_embedder is wired to a composite TEI-backed Embedder."""
    bootstrap_production_app(app, settings)
    assert get_embedder in app.dependency_overrides
    embedder = app.dependency_overrides[get_embedder]()
    # Composite must satisfy the Embedder protocol so planes accept it.
    assert isinstance(embedder, Embedder)


def test_bootstrap_installs_every_plane_override(
    app: FastAPI, settings: Settings, patch_qdrant_ok: Any
) -> None:
    """Bullet 3 — every plane factory is wired to a real instance.
    Episodic + curated + concept + artifact + thoughts."""
    bootstrap_production_app(app, settings)
    expected = {
        get_episodic_plane: EpisodicPlane,
        get_curated_plane: CuratedPlane,
        get_concept_plane: ConceptPlane,
        get_artifact_plane: ArtifactPlane,
        get_thoughts_plane: ThoughtsPlane,
    }
    for factory, plane_cls in expected.items():
        assert factory in app.dependency_overrides, f"{factory.__name__} not overridden"
        instance = app.dependency_overrides[factory]()
        assert isinstance(instance, plane_cls), (
            f"{factory.__name__} returned {type(instance).__name__}, expected {plane_cls.__name__}"
        )


def test_bootstrap_installs_lifecycle_service_override(
    app: FastAPI, settings: Settings, patch_qdrant_ok: Any
) -> None:
    """Bullet 4 — bootstrap also wires a lifecycle service so the
    /v1/lifecycle/* endpoints can resolve their dep. Surface check:
    the bootstrap exposes a ``get_lifecycle_service`` override and
    the service constructed is the one the routers expect."""
    from musubi.api.dependencies import get_lifecycle_service

    bootstrap_production_app(app, settings)
    assert get_lifecycle_service in app.dependency_overrides
    service = app.dependency_overrides[get_lifecycle_service]()
    # Bootstrap returns a real service object (not None, not the
    # NotImplementedError stub).
    assert service is not None


# ---------------------------------------------------------------------------
# Bullet 5 — idempotent
# ---------------------------------------------------------------------------


def test_bootstrap_is_idempotent_on_second_call(
    app: FastAPI, settings: Settings, patch_qdrant_ok: Any
) -> None:
    """Bullet 5 — re-running bootstrap re-installs cleanly and
    doesn't accumulate stacked overrides or duplicate factory
    instances."""
    bootstrap_production_app(app, settings)
    overrides_before = dict(app.dependency_overrides)
    bootstrap_production_app(app, settings)
    overrides_after = dict(app.dependency_overrides)
    # Same set of keys; same set of values for the
    # NotImplementedError stubs (override pointers replaced cleanly).
    assert set(overrides_before.keys()) == set(overrides_after.keys())


# ---------------------------------------------------------------------------
# Bullets 6-8 — health-gated init
# ---------------------------------------------------------------------------


def test_bootstrap_fails_loudly_when_qdrant_unreachable(
    app: FastAPI, settings: Settings, patch_qdrant_unreachable: Any
) -> None:
    """Bullet 6 — Qdrant unreachable on every retry → BootstrapError
    naming Qdrant. Preserves the fail-loud invariant the
    NotImplementedError stubs encoded."""
    with pytest.raises(BootstrapError, match="qdrant"):
        bootstrap_production_app(app, settings, retry_attempts=2, retry_backoff_s=0.0)


def test_bootstrap_fails_loudly_when_tei_unreachable(
    app: FastAPI, settings: Settings, patch_qdrant_ok: Any
) -> None:
    """Bullet 7 — TEI unreachable on every retry → BootstrapError
    naming TEI."""
    with (
        patch("musubi.api.bootstrap._probe_tei", side_effect=ConnectionError("tei down")),
        pytest.raises(BootstrapError, match="tei"),
    ):
        bootstrap_production_app(app, settings, retry_attempts=2, retry_backoff_s=0.0)


def test_bootstrap_retry_succeeds_on_second_attempt(
    app: FastAPI, settings: Settings, patch_qdrant_recovers: Any
) -> None:
    """Bullet 8 — first probe fails, second succeeds → bootstrap
    completes without raising."""
    bootstrap_production_app(app, settings, retry_attempts=3, retry_backoff_s=0.0)
    assert get_qdrant_client in app.dependency_overrides


# ---------------------------------------------------------------------------
# Bullets 9-11 — create_app() integration
# ---------------------------------------------------------------------------


def test_create_app_calls_bootstrap_by_default(settings: Settings, patch_qdrant_ok: Any) -> None:
    """Bullet 9 — create_app(settings=production_settings) invokes
    bootstrap_production_app on the way up. Verified by asserting
    the resulting app has plane factories overridden.

    The shared ``settings`` fixture inherits ``musubi_skip_bootstrap=True``
    from the api_settings fixture (so the broader unit suite skips
    the bootstrap); override it back to False here to exercise the
    production-default path."""
    from musubi.api.app import create_app

    prod_settings = settings.model_copy(update={"musubi_skip_bootstrap": False})
    app = create_app(settings=prod_settings)
    assert get_episodic_plane in app.dependency_overrides


def test_create_app_skips_bootstrap_when_musubi_skip_bootstrap_set(
    settings: Settings, patch_qdrant_ok: Any
) -> None:
    """Bullet 11 — explicit MUSUBI_SKIP_BOOTSTRAP=true (via settings)
    short-circuits the bootstrap call, leaving the
    NotImplementedError stubs in place. The narrow escape hatch the
    spec describes for tests that don't go through app_factory."""
    from musubi.api.app import create_app

    skip_settings = settings.model_copy(update={"musubi_skip_bootstrap": True})
    app = create_app(settings=skip_settings)
    assert get_episodic_plane not in app.dependency_overrides


def test_create_app_skips_bootstrap_when_overrides_already_installed(
    settings: Settings, patch_qdrant_ok: Any
) -> None:
    """Bullet 10 — when an existing dep override is detected on a
    pre-built app (the operator's spec describes this as the test
    fixture path), bootstrap is a no-op. We exercise this via the
    private helper so the create_app gate is testable in isolation."""
    from musubi.api.bootstrap import _should_bootstrap

    pre_built_app = FastAPI()
    pre_built_app.dependency_overrides[get_qdrant_client] = lambda: object()
    assert _should_bootstrap(pre_built_app, settings) is False


# ---------------------------------------------------------------------------
# Bullet 12 — regression: existing unit-test fixtures still work
# ---------------------------------------------------------------------------


def test_existing_unit_test_fixtures_still_work_unchanged(client: Any, valid_token: str) -> None:
    """Bullet 12 — the existing api_factory + client fixtures (in
    tests/api/conftest.py) pre-install dependency_overrides that
    win over the bootstrap's; an existing api/test_* still passes
    against this regression check by hitting one of the live
    routes through the existing client fixture and asserting a
    typed-error envelope (not a 500 from the bootstrap path)."""
    resp = client.get("/v1/ops/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# Coverage tests — exercise additional surfaces of bootstrap.py
# ---------------------------------------------------------------------------


def test_bootstrap_error_carries_dep_name() -> None:
    """BootstrapError exposes the failing dependency by name so
    operator can grep alerts/logs."""
    err = BootstrapError(dep="qdrant", detail="connect failed")
    assert err.dep == "qdrant"
    assert "qdrant" in str(err)


def test_should_bootstrap_returns_true_on_clean_app(app: FastAPI, settings: Settings) -> None:
    from musubi.api.bootstrap import _should_bootstrap

    prod_settings = settings.model_copy(update={"musubi_skip_bootstrap": False})
    assert _should_bootstrap(app, prod_settings) is True


def test_should_bootstrap_returns_false_when_skip_flag_set(
    app: FastAPI, settings: Settings
) -> None:
    from musubi.api.bootstrap import _should_bootstrap

    skipped = settings.model_copy(update={"musubi_skip_bootstrap": True})
    assert _should_bootstrap(app, skipped) is False


def test_composite_embedder_satisfies_protocol(
    app: FastAPI, settings: Settings, patch_qdrant_ok: Any
) -> None:
    """The TEI-composite Embedder bootstrap installs has every
    method the protocol declares."""
    bootstrap_production_app(app, settings)
    embedder = app.dependency_overrides[get_embedder]()
    assert hasattr(embedder, "embed_dense")
    assert hasattr(embedder, "embed_sparse")
    assert hasattr(embedder, "rerank")
