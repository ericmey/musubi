"""Structured-logging helpers — JSON formatter + request-id contextvar.

Per [[09-operations/observability]] § Logs. Every log line is a single
JSON object; correlation IDs propagate via :data:`request_id_var`
(populated by the API's correlation-id middleware on each inbound
request). :func:`redact_token_filter` is a logging filter that
scrubs JWT-shaped strings out of the rendered message before emit.
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
"""Per-request correlation id. The API's correlation-id middleware
sets this for the lifetime of each request; structured log records
pull it via :class:`StructuredJsonFormatter`."""


_BUILTIN_RECORD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class StructuredJsonFormatter(logging.Formatter):
    """One JSON object per log record — the spec's exact shape."""

    def format(self, record: logging.LogRecord) -> str:
        # Apply the redaction filter inline so emit-time scrubbing is
        # unconditional even if the caller forgot to attach the filter.
        redact_token_filter(record)
        payload: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "service": record.name,
            "msg": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid is not None:
            payload["request_id"] = rid
        # Pull any extra fields the caller attached (record.namespace,
        # record.object_id, etc.) into the top-level payload.
        for k, v in record.__dict__.items():
            if k in _BUILTIN_RECORD_FIELDS:
                continue
            if k == "message":
                continue
            payload.setdefault(k, v)
        if record.exc_info is not None:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_TOKEN_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_.-]+")
"""JWT-shape detector: starts with ``eyJ`` (header `{"typ":...` base64url),
followed by base64-ish chars and at least one dot. False positives
cost log noise; false negatives cost a leaked token, so we err
toward over-scrubbing."""


def redact_token_filter(record: logging.LogRecord) -> bool:
    """Logging filter — replaces JWT-shaped substrings with ``[REDACTED]``.

    Returns ``True`` so the record continues through the handler
    chain. Idempotent: running twice yields the same scrubbed message.
    Mutates ``record.msg`` in place; if the message was a non-string
    (e.g. an exception), it's left untouched.
    """
    msg = record.msg
    if isinstance(msg, str) and _TOKEN_PATTERN.search(msg):
        record.msg = _TOKEN_PATTERN.sub("[REDACTED]", msg)
    # Also scrub the rendered .args path so f-string-style logging stays safe.
    if isinstance(record.args, tuple):
        scrubbed: list[object] = []
        for a in record.args:
            if isinstance(a, str) and _TOKEN_PATTERN.search(a):
                scrubbed.append(_TOKEN_PATTERN.sub("[REDACTED]", a))
            else:
                scrubbed.append(a)
        record.args = tuple(scrubbed)
    return True


__all__ = [
    "StructuredJsonFormatter",
    "redact_token_filter",
    "request_id_var",
]
