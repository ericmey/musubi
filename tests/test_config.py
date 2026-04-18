"""Test contract for slice-config.

Realises the behaviour expected of the sole config surface in Musubi:
``musubi.config.get_settings()``. Every other module imports settings from
here; ``os.environ`` reads elsewhere are forbidden by the guardrails.

Scope of these tests:

1. ``get_settings()`` returns the same cached instance across calls.
2. Values are read from the process environment and from a ``.env`` file.
3. Required settings without defaults fail fast with a clear error.
4. Secret values are masked in the model's ``repr()``.
5. String env values are coerced to the declared Python type (int, bool, Path).
6. Invalid values are rejected with a pydantic ``ValidationError``.
7. Process environment overrides ``.env`` contents.
8. Spec-defaulted fields (feature flags, ports) load without explicit input.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from musubi import config as config_module
from musubi.config import Settings, get_settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_ENV_KEYS: tuple[str, ...] = (
    # Qdrant
    "QDRANT_HOST",
    "QDRANT_PORT",
    "QDRANT_API_KEY",
    # Inference
    "TEI_DENSE_URL",
    "TEI_SPARSE_URL",
    "TEI_RERANKER_URL",
    "OLLAMA_URL",
    "EMBEDDING_MODEL",
    "SPARSE_MODEL",
    "RERANKER_MODEL",
    "LLM_MODEL",
    # Core
    "BRAIN_PORT",
    "VAULT_PATH",
    "ARTIFACT_BLOB_PATH",
    "LIFECYCLE_SQLITE_PATH",
    "LOG_DIR",
    # Auth
    "JWT_SIGNING_KEY",
    "OAUTH_AUTHORITY",
    # Feature flags
    "MUSUBI_GRPC",
    "MUSUBI_ALLOW_PLAINTEXT",
)


@pytest.fixture
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Drop every Musubi env var so each test starts from a known state."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Defensive: guarantee no shell export leaks across tests.
    yield


@pytest.fixture
def _reset_cache() -> Iterator[None]:
    """Clear the ``lru_cache`` on ``get_settings`` between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def minimal_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _clean_env: None
) -> Iterator[Path]:
    """Populate every required env var with a sane test value.

    Returns the ``tmp_path`` so tests can cwd into it and drop a ``.env``.
    """
    monkeypatch.setenv("QDRANT_HOST", "qdrant")
    monkeypatch.setenv("QDRANT_PORT", "6333")
    monkeypatch.setenv("QDRANT_API_KEY", "test-qdrant-key")
    monkeypatch.setenv("TEI_DENSE_URL", "http://tei-dense")
    monkeypatch.setenv("TEI_SPARSE_URL", "http://tei-sparse")
    monkeypatch.setenv("TEI_RERANKER_URL", "http://tei-reranker")
    monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    monkeypatch.setenv("SPARSE_MODEL", "naver/splade-v3")
    monkeypatch.setenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
    monkeypatch.setenv("LLM_MODEL", "qwen2.5:7b-instruct-q4_K_M")
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("ARTIFACT_BLOB_PATH", str(tmp_path / "artifacts"))
    monkeypatch.setenv("LIFECYCLE_SQLITE_PATH", str(tmp_path / "lifecycle.sqlite"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "log"))
    monkeypatch.setenv("JWT_SIGNING_KEY", "test-jwt-secret")
    monkeypatch.setenv("OAUTH_AUTHORITY", "https://auth.example.test")
    yield tmp_path


# ---------------------------------------------------------------------------
# 1. Singleton via lru_cache
# ---------------------------------------------------------------------------


def test_get_settings_returns_same_instance_across_calls(
    minimal_env: Path, _reset_cache: None
) -> None:
    a = get_settings()
    b = get_settings()
    assert a is b, "get_settings must be cached so imports share one instance"


# ---------------------------------------------------------------------------
# 2. Reads from both env and .env
# ---------------------------------------------------------------------------


def test_settings_reads_from_env_and_from_dotenv_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _clean_env: None,
    _reset_cache: None,
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            [
                "QDRANT_HOST=qdrant-from-dotenv",
                "QDRANT_PORT=6333",
                "QDRANT_API_KEY=from-dotenv",
                "TEI_DENSE_URL=http://tei-dense",
                "TEI_SPARSE_URL=http://tei-sparse",
                "TEI_RERANKER_URL=http://tei-reranker",
                "OLLAMA_URL=http://ollama:11434",
                "EMBEDDING_MODEL=BAAI/bge-m3",
                "SPARSE_MODEL=naver/splade-v3",
                "RERANKER_MODEL=BAAI/bge-reranker-v2-m3",
                "LLM_MODEL=qwen2.5:7b-instruct-q4_K_M",
                f"VAULT_PATH={tmp_path / 'vault'}",
                f"ARTIFACT_BLOB_PATH={tmp_path / 'artifacts'}",
                f"LIFECYCLE_SQLITE_PATH={tmp_path / 'lifecycle.sqlite'}",
                f"LOG_DIR={tmp_path / 'log'}",
                "JWT_SIGNING_KEY=jwt-from-dotenv",
                "OAUTH_AUTHORITY=https://auth.example.test",
            ]
        )
    )
    # Point pydantic-settings at this .env regardless of process CWD.
    monkeypatch.setenv("MUSUBI_DOTENV", str(dotenv))
    settings = get_settings()
    assert settings.qdrant_host == "qdrant-from-dotenv"
    assert settings.jwt_signing_key.get_secret_value() == "jwt-from-dotenv"


# ---------------------------------------------------------------------------
# 3. Required-but-missing → clear error
# ---------------------------------------------------------------------------


def test_missing_required_setting_fails_fast_with_clear_error(
    monkeypatch: pytest.MonkeyPatch, _clean_env: None, _reset_cache: None
) -> None:
    # Point at a nonexistent dotenv so only env vars matter.
    monkeypatch.setenv("MUSUBI_DOTENV", "/nonexistent/.env.not-there")
    with pytest.raises(ValidationError) as excinfo:
        get_settings()
    message = str(excinfo.value)
    # Must name at least one required field so the operator knows what to set.
    assert "jwt_signing_key" in message.lower() or "qdrant_host" in message.lower()


# ---------------------------------------------------------------------------
# 4. Secret masking in repr()
# ---------------------------------------------------------------------------


def test_secret_values_masked_in_repr(minimal_env: Path, _reset_cache: None) -> None:
    settings = get_settings()
    rendered = repr(settings)
    assert "test-jwt-secret" not in rendered
    assert "test-qdrant-key" not in rendered
    # ``SecretStr`` displays as ``**********`` in repr — assert the marker shows up.
    assert "**********" in rendered


# ---------------------------------------------------------------------------
# 5. Type coercion from strings
# ---------------------------------------------------------------------------


def test_type_coerced_from_string(minimal_env: Path, _reset_cache: None) -> None:
    settings = get_settings()
    # int from string
    assert isinstance(settings.qdrant_port, int)
    assert settings.qdrant_port == 6333
    # Path from string
    assert isinstance(settings.vault_path, Path)
    assert settings.vault_path == minimal_env / "vault"


def test_bool_coerced_from_string(
    monkeypatch: pytest.MonkeyPatch, minimal_env: Path, _reset_cache: None
) -> None:
    monkeypatch.setenv("MUSUBI_GRPC", "true")
    monkeypatch.setenv("MUSUBI_ALLOW_PLAINTEXT", "false")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.musubi_grpc is True
    assert settings.musubi_allow_plaintext is False


# ---------------------------------------------------------------------------
# 6. Invalid values → pydantic ValidationError
# ---------------------------------------------------------------------------


def test_invalid_values_rejected_with_pydantic_validation_error(
    monkeypatch: pytest.MonkeyPatch, minimal_env: Path, _reset_cache: None
) -> None:
    monkeypatch.setenv("QDRANT_PORT", "not-a-number")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()


def test_invalid_url_rejected(
    monkeypatch: pytest.MonkeyPatch, minimal_env: Path, _reset_cache: None
) -> None:
    monkeypatch.setenv("TEI_DENSE_URL", "not a url at all")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()


# ---------------------------------------------------------------------------
# 7. Env overrides .env
# ---------------------------------------------------------------------------


def test_env_overrides_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _clean_env: None,
    _reset_cache: None,
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            [
                "QDRANT_HOST=from-dotenv",
                "QDRANT_PORT=6333",
                "QDRANT_API_KEY=from-dotenv",
                "TEI_DENSE_URL=http://tei-dense",
                "TEI_SPARSE_URL=http://tei-sparse",
                "TEI_RERANKER_URL=http://tei-reranker",
                "OLLAMA_URL=http://ollama:11434",
                "EMBEDDING_MODEL=BAAI/bge-m3",
                "SPARSE_MODEL=naver/splade-v3",
                "RERANKER_MODEL=BAAI/bge-reranker-v2-m3",
                "LLM_MODEL=qwen2.5:7b-instruct-q4_K_M",
                f"VAULT_PATH={tmp_path / 'vault'}",
                f"ARTIFACT_BLOB_PATH={tmp_path / 'artifacts'}",
                f"LIFECYCLE_SQLITE_PATH={tmp_path / 'lifecycle.sqlite'}",
                f"LOG_DIR={tmp_path / 'log'}",
                "JWT_SIGNING_KEY=from-dotenv",
                "OAUTH_AUTHORITY=https://auth.example.test",
            ]
        )
    )
    monkeypatch.setenv("MUSUBI_DOTENV", str(dotenv))
    # Process env overrides the same key set in the file.
    monkeypatch.setenv("QDRANT_HOST", "from-process-env")
    settings = get_settings()
    assert settings.qdrant_host == "from-process-env"


# ---------------------------------------------------------------------------
# 8. Default values present for optional fields
# ---------------------------------------------------------------------------


def test_default_values_present_where_spec_allows(minimal_env: Path, _reset_cache: None) -> None:
    settings = get_settings()
    # BRAIN_PORT defaults to 8100 per compose-stack.
    assert settings.brain_port == 8100
    # Feature flags default to false per compose-stack.
    assert settings.musubi_grpc is False
    assert settings.musubi_allow_plaintext is False


# ---------------------------------------------------------------------------
# Operational: no stray os.environ reads leak around get_settings
# ---------------------------------------------------------------------------


def test_get_settings_lives_in_config_module() -> None:
    """Sanity: the public accessor exists on the expected module path.

    Guards against drift where other modules shadow the accessor.
    """
    assert hasattr(config_module, "get_settings")
    assert hasattr(config_module, "Settings")
    assert issubclass(Settings, config_module.Settings)


def test_settings_frozen_after_load(minimal_env: Path, _reset_cache: None) -> None:
    """Settings are effectively read-only; mutation raises."""
    settings = get_settings()
    with pytest.raises((ValidationError, TypeError, AttributeError)):
        settings.qdrant_host = "mutated"


def test_no_module_imports_os_environ_for_config() -> None:
    """Smoke: ``os.environ`` must not be touched to read config outside musubi/config.py."""
    import importlib
    import pkgutil

    import musubi

    banned_modules: list[str] = []
    for mod_info in pkgutil.walk_packages(musubi.__path__, prefix="musubi."):
        if mod_info.name in {"musubi.config", "musubi.settings"}:
            continue
        module = importlib.import_module(mod_info.name)
        src = getattr(module, "__file__", None)
        if not src:
            continue
        text = Path(src).read_text(encoding="utf-8")
        if "os.environ" in text or "os.getenv" in text:
            banned_modules.append(mod_info.name)
    assert banned_modules == [], (
        f"modules reading os.environ/os.getenv outside musubi.config: {banned_modules}"
    )


def test_repr_includes_non_secret_fields(minimal_env: Path, _reset_cache: None) -> None:
    """Non-secret fields show through for debuggability; secrets do not."""
    settings = get_settings()
    rendered = repr(settings)
    assert "qdrant_host=" in rendered
    assert "brain_port=8100" in rendered


def test_settings_values_match_env(
    monkeypatch: pytest.MonkeyPatch, minimal_env: Path, _reset_cache: None
) -> None:
    """Round-trip: what we set in env matches what get_settings returns."""
    monkeypatch.setenv("QDRANT_HOST", "host-under-test")
    get_settings.cache_clear()
    s = get_settings()
    assert s.qdrant_host == "host-under-test"
    # Comparison against actual process env must use the accessor, not os.environ:
    assert s.qdrant_host == os.environ["QDRANT_HOST"]
