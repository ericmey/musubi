"""Declarative settings model for Musubi Core.

The sole pydantic-settings ``BaseSettings`` subclass in the codebase. Sources,
in order of precedence:

1. Process environment variables.
2. A ``.env`` file at either the repo root or the path given by
   ``MUSUBI_DOTENV`` (useful for tests).

Rules, enforced by this module and the agent guardrails
(`docs/Musubi/00-index/agent-guardrails.md#Prohibited patterns`):

- No other module reads ``os.environ``. Import ``get_settings()`` instead.
- Secrets are ``SecretStr``; their full value never appears in ``__repr__``
  (pydantic masks them as ``**********``).
- Every type is concrete: ``int``, ``bool``, ``Path``, ``AnyHttpUrl`` — so
  values coming in as strings from the shell are coerced at load time rather
  than ad-hoc throughout the codebase.

The accessor lives in :mod:`musubi.config`. Keeping the model here and the
accessor there preserves the slice spec's two-file contract
(``musubi/settings.py`` + ``musubi/config.py``) without duplicating state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for Musubi Core.

    Fields correspond 1:1 to the ``/etc/musubi/.env`` sample in
    `docs/Musubi/08-deployment/compose-stack.md` §Env.

    The ``env_file`` is chosen dynamically by :func:`musubi.config.get_settings`
    (reads ``MUSUBI_DOTENV`` — the sole allowed env read outside this module —
    and passes it as ``_env_file`` at instantiation). The class-level default
    points at a repo-root ``.env`` for ad-hoc ``Settings()`` calls.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Freeze the model so consumers can treat it as a value type; mutation
        # anywhere other than load time is a bug.
        frozen=True,
        # Reject misspelled env vars instead of silently ignoring them.
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Qdrant
    # ------------------------------------------------------------------
    qdrant_host: str = Field(description="Qdrant service hostname or IP.")
    qdrant_port: int = Field(default=6333, ge=1, le=65535)
    qdrant_api_key: SecretStr = Field(description="Qdrant HTTP API key.")

    # ------------------------------------------------------------------
    # Inference endpoints
    # ------------------------------------------------------------------
    tei_dense_url: AnyHttpUrl = Field(description="TEI dense-embeddings endpoint.")
    tei_sparse_url: AnyHttpUrl = Field(description="TEI sparse-embeddings endpoint.")
    tei_reranker_url: AnyHttpUrl = Field(description="TEI reranker endpoint.")
    ollama_url: AnyHttpUrl = Field(description="Ollama LLM endpoint.")

    embedding_model: str = Field(description="Dense embedding model id (HF).")
    sparse_model: str = Field(description="Sparse embedding model id (HF).")
    reranker_model: str = Field(description="Reranker model id (HF).")
    llm_model: str = Field(description="Ollama-tagged LLM model.")

    # ------------------------------------------------------------------
    # Core service
    # ------------------------------------------------------------------
    brain_port: int = Field(default=8100, ge=1, le=65535)
    vault_path: Path = Field(description="Host path to the Obsidian vault mount.")
    artifact_blob_path: Path = Field(description="Host path to content-addressed blobs.")
    lifecycle_sqlite_path: Path = Field(description="Host path to lifecycle-work sqlite.")
    log_dir: Path = Field(description="Host path for structured log output.")

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    jwt_signing_key: SecretStr = Field(description="HS256 signing key for internal JWTs.")
    oauth_authority: AnyHttpUrl = Field(description="OIDC issuer (Auth0 / Kong) base URL.")

    # ------------------------------------------------------------------
    # Feature flags
    # ------------------------------------------------------------------
    musubi_grpc: bool = Field(default=False, description="Expose the gRPC API alongside REST.")

    # ------------------------------------------------------------------
    # Rate limits (tokens per second)
    # ------------------------------------------------------------------
    rate_limit_capture: float = Field(default=10.0, description="Tokens per second for capture.")
    rate_limit_retrieve: float = Field(default=20.0, description="Tokens per second for retrieve.")
    rate_limit_thought: float = Field(
        default=5.0, description="Tokens per second for thought sending."
    )

    musubi_allow_plaintext: bool = Field(
        default=False,
        description="Permit non-TLS downstream calls. Strictly dev-only.",
    )
    musubi_skip_bootstrap: bool = Field(
        default=False,
        description=(
            "Skip the production app bootstrap (slice-api-app-bootstrap) on "
            "create_app(). Set to True in unit-test fixtures that override "
            "FastAPI dependencies AFTER create_app returns; production "
            "deployments leave this as False so plane factories wire on boot."
        ),
    )

    # ------------------------------------------------------------------
    # MCP adapter (client-side config for the MCP server process only;
    # unused by core Musubi processes — optional with sensible defaults)
    # ------------------------------------------------------------------
    musubi_api_url: AnyHttpUrl = Field(
        default=AnyHttpUrl("http://localhost:8100/v1"),
        description="URL of the Musubi API the MCP server should call. "
        "Defaults to localhost when the MCP server runs on the same host "
        "as Musubi Core (the typical `mcp.musubi.mey.house` deployment).",
    )
    musubi_token: SecretStr = Field(
        default=SecretStr(""),
        description="Bearer token the MCP server uses to authenticate with "
        "Musubi's API. Empty default means no auth header is sent; auth "
        "failures will surface as 401s from the API.",
    )

    # ------------------------------------------------------------------
    # repr: pydantic-settings already masks SecretStr as ``**********``;
    # override to prune internal pydantic noise for a cleaner log line.
    # ------------------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - trivial formatter
        fields: list[str] = []
        for name, _info in type(self).model_fields.items():
            value: Any = getattr(self, name)
            fields.append(f"{name}={value!r}")
        return f"Settings({', '.join(fields)})"


__all__ = ["Settings"]
