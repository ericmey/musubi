"""Ingestion service layer.

:class:`musubi.ingestion.capture.CaptureService` is a capture layer
over the episodic plane. It is not wired into the HTTP API: the
``POST /v1/episodic`` route builds the memory and calls
``EpisodicPlane.create`` directly. The service owns:

- Per-plane dedup configuration (``DEFAULT_DEDUP_THRESHOLDS``).
- Per-(token, namespace) idempotency cache (``IngestionIdempotencyCache``)
  — distinct from the API's middleware cache so different bearers with
  the same key don't collide.
- Bounded retry around plane writes for transient Qdrant blips.
- Lifecycle event emission on every successful capture so the audit
  ledger records the ingestion provenance.

See [[06-ingestion/capture]] for the spec.
"""

from musubi.ingestion.capture import (
    DEFAULT_DEDUP_THRESHOLDS,
    CaptureError,
    CaptureRequest,
    CaptureResult,
    CaptureService,
    IngestionIdempotencyCache,
    is_dedup_enabled,
)

__all__ = [
    "DEFAULT_DEDUP_THRESHOLDS",
    "CaptureError",
    "CaptureRequest",
    "CaptureResult",
    "CaptureService",
    "IngestionIdempotencyCache",
    "is_dedup_enabled",
]
