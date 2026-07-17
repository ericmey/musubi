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

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
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
    api_workers: int = Field(
        default=1,
        ge=1,
        le=1,
        description=(
            "API worker count. Pinned to 1 (fail-closed): the idempotency cache is in-memory "
            "and process-local, so >1 worker would tear it silently. A multi-worker deployment "
            "must first move the cache to a shared backend."
        ),
    )
    web_concurrency: int = Field(
        default=1,
        ge=1,
        description=(
            "Reads the standard WEB_CONCURRENCY uvicorn/gunicorn worker signal. Uncapped here so "
            "the value is visible; create_app REJECTS >1 (process-local idempotency cache). "
            "Routing config through Settings keeps os.environ out of app code."
        ),
    )
    vault_path: Path = Field(description="Host path to the Obsidian vault mount.")
    artifact_blob_path: Path = Field(description="Host path to content-addressed blobs.")
    lifecycle_sqlite_path: Path = Field(description="Host path to lifecycle-work sqlite.")
    idempotency_receipt_sqlite_path: Path | None = Field(
        default=None,
        description=(
            "Optional host-path override for the durable completed-response receipt ledger. "
            "When omitted, create_app derives a sibling of lifecycle_sqlite_path. Independent "
            "from the ordinary idempotency replay TTL."
        ),
    )
    lifecycle_sqlite_busy_timeout_ms: int = Field(
        default=5000,
        ge=0,
        le=600_000,
        description=(
            "SQLite busy_timeout (ms) for every shared lifecycle-store connection "
            "(WAL). Default 5000. A value of 0 disables waiting — SQLite returns "
            "SQLITE_BUSY immediately on contention; a deliberate operator override, "
            "not a fail-closed guard."
        ),
    )
    lifecycle_pending_cap: int = Field(
        default=10_000,
        gt=0,
        description=(
            "Global cap on non-terminal (PENDING/APPLIED) lifecycle_outbox rows. The "
            "coordinator's atomic admission rejects a new transition with cap_exceeded "
            "once the backlog reaches this cap. Positive int; there is no unbounded option."
        ),
    )
    lifecycle_lease_ttl_s: float = Field(
        default=30.0,
        gt=0,
        description=(
            "Reconciler lease TTL (seconds). A claim stamps lease_expires_epoch = now + this; a "
            "row whose lease has expired is reclaimable by another worker. Positive finite float."
        ),
    )
    lifecycle_reconcile_interval_s: int = Field(
        default=5,
        gt=0,
        description=(
            "Interval (seconds) between reconcile_once passes in the lifecycle worker. Positive "
            "int (accepted source-cut §G)."
        ),
    )
    lifecycle_backoff_base_s: float = Field(
        default=1.0,
        gt=0,
        description=(
            "Base of the reconciler's bounded exponential retry backoff (seconds). A transient/"
            "unknown apply reschedules next_attempt_epoch = now + min(base * 2**attempts, max). "
            "Positive finite float."
        ),
    )
    lifecycle_backoff_max_s: float = Field(
        default=300.0,
        gt=0,
        description=(
            "Ceiling of the reconciler's retry backoff (seconds); must be >= "
            "lifecycle_backoff_base_s. Positive finite float."
        ),
    )
    lifecycle_cleanup_retention_s: int = Field(
        default=30 * 86400,
        gt=0,
        description=(
            "Retention window (seconds) for terminal lifecycle outbox rows. "
            "The worker deletes only rows strictly older than this window."
        ),
    )
    lifecycle_cleanup_batch: int = Field(
        default=1000,
        gt=0,
        description="Maximum terminal lifecycle outbox rows deleted per reconcile pass.",
    )
    lifecycle_readiness_max_reconcile_failures: int = Field(
        default=3,
        gt=0,
        description=(
            "Consecutive reconcile failures tolerated before the lifecycle-worker "
            "readiness gauge is forced to zero."
        ),
    )
    log_dir: Path = Field(description="Host path for structured log output.")

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    jwt_signing_key: SecretStr = Field(description="HS256 signing key for internal JWTs.")
    oauth_authority: AnyHttpUrl = Field(description="OIDC issuer (Auth0 / Kong) base URL.")

    # AUTH-001: per-agent namespace-exclusion list. The default
    # ``salesai`` is a mandatory baseline and cannot be removed.
    # Per-agent settings may add more exclusions. The enforcement
    # seam (auth.scopes.enforce_namespace_policy) composes this
    # at request time directly from Settings.
    default_excluded_namespaces: frozenset[str] = Field(
        default=frozenset({"salesai"}),
        description=(
            "Mandatory baseline exclusion list. Always excluded from "
            "recall; Settings overrides cannot subtract from this set."
        ),
    )

    @field_validator("default_excluded_namespaces", mode="after")
    @classmethod
    def _enforce_mandatory_salesai(cls, v: frozenset[str]) -> frozenset[str]:
        return v | frozenset({"salesai"})

    per_agent_excluded_namespaces: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        description=(
            "Per-agent additional exclusions keyed by stable "
            "authenticated subject OR presence. Both contribute via "
            "union; the per-agent exclusion is additive on top of "
            "default_excluded_namespaces."
        ),
    )

    # ------------------------------------------------------------------
    # Feature flags
    # ------------------------------------------------------------------
    musubi_grpc: bool = Field(default=False, description="Expose the gRPC API alongside REST.")

    lifecycle_metrics_port: int = Field(
        default=8101,
        ge=1,
        le=65535,
        description=(
            "Port the lifecycle-worker exposes Prometheus `/metrics` on. "
            "Worker has no FastAPI surface, so this is a stdlib HTTP server "
            "started by `musubi.observability.scrape_server.start_metrics_server` "
            "during worker boot. Bound on 0.0.0.0; production deploys reach it "
            "via the compose internal network and do not host-bind."
        ),
    )

    musubi_artifact_archival_enabled: bool = Field(
        default=False,
        description=(
            "Opt in to the artifact-archival lifecycle sweep. When False (default), "
            "`demotion_artifact` is a no-op. When True, artifacts older than "
            "`DEMOTION_ARTIFACT_AGE_DAYS` (180d) and not referenced by any memory "
            "are transitioned to state=archived. Blob bytes are preserved; "
            "storage reclamation is a separate follow-up (issue #222)."
        ),
    )

    # Rate limits — per-bucket, per-minute — live in
    # `src/musubi/api/rate_limit.py::DEFAULT_BUCKETS`. See ADR 0027 for
    # the rationale on why they are not tunable via settings.

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
        "as Musubi Core (the typical `mcp.musubi.example.local` deployment).",
    )
    musubi_token: SecretStr = Field(
        default=SecretStr(""),
        description="Bearer token the MCP server uses to authenticate with "
        "Musubi's API. Empty default means no auth header is sent; auth "
        "failures will surface as 401s from the API.",
    )

    # ------------------------------------------------------------------
    # OpenTelemetry tracing (server-side). Per
    # [[09-operations/observability]] § Tracing. All fields are optional
    # and default to "off" / unset — the server runs unchanged when
    # traces aren't wanted. Field names match the canonical OTel env
    # vars so existing OTel docs/tools apply directly.
    # ------------------------------------------------------------------
    otel_exporter_otlp_endpoint: str = Field(
        default="",
        description="OTLP/gRPC endpoint for span export "
        "(e.g. `http://shiori.mey.house:4317`). Empty disables tracing.",
    )
    otel_service_name: str = Field(
        default="musubi-core",
        description="Resource `service.name` attribute on emitted spans. "
        "Aligned with the rest of the fleet so Tempo + Mimir labels match.",
    )
    otel_service_namespace: str = Field(
        default="musubi",
        description="Resource `service.namespace` attribute on emitted spans.",
    )
    otel_deployment_environment: str = Field(
        default="harem-world",
        description="Resource `deployment.environment` attribute.",
    )
    otel_host_name: str = Field(
        default="",
        description="Override the host.name resource attribute. "
        "Empty (default) means `init_tracing` derives it from the "
        "container/host hostname.",
    )
    musubi_service_version: str = Field(
        default="",
        description="Resource `service.version` attribute. Typically set "
        "by the deploy pipeline to the git sha or tag of the running image.",
    )

    @model_validator(mode="after")
    def _validate_backoff_bounds(self) -> Settings:
        """The reconciler's retry backoff ceiling must not be below its base."""
        if self.lifecycle_backoff_max_s < self.lifecycle_backoff_base_s:
            raise ValueError(
                f"lifecycle_backoff_max_s ({self.lifecycle_backoff_max_s}) must be >= "
                f"lifecycle_backoff_base_s ({self.lifecycle_backoff_base_s})"
            )
        return self

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
