"""Minimal PII redaction pass for the LiveKit voice adapter.

Per [[07-interfaces/livekit-adapter]] § Privacy. Off by default; the
adapter only invokes this when ``LiveKitAdapterConfig.redact_pii=True``.
Patterns cover the common voice-transcript cases (email, US SSN,
phone-ish digit groups). The full canonical redactor lives in
[[10-security/redaction]] and is not in scope for this slice.
"""

from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

_PII_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Emails.
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    # US SSN-shaped digit groups.
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # Phone-ish 10-digit groups with separators.
    re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"),
)


def redact_pii(text: str) -> str:
    """Return ``text`` with email / SSN / phone shapes replaced by
    ``[REDACTED]``. Idempotent — running twice is a no-op."""
    out = text
    for pattern in _PII_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out
