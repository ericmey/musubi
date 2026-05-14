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
        # OTel correlation: LoggingInstrumentor (when installed by
        # `musubi.observability.tracing.init_tracing`) sets
        # ``otelTraceID``/``otelSpanID`` on every record from the current
        # span context. Promote them to top-level ``trace_id``/``span_id``
        # so the JSON log shape matches the spec language and lines up
        # with Grafana's logs-to-traces correlation defaults.
        otel_trace_id = getattr(record, "otelTraceID", None)
        if otel_trace_id and otel_trace_id != "0" * 32:
            payload["trace_id"] = otel_trace_id
        otel_span_id = getattr(record, "otelSpanID", None)
        if otel_span_id and otel_span_id != "0" * 16:
            payload["span_id"] = otel_span_id
        # Pull any extra fields the caller attached (record.namespace,
        # record.object_id, etc.) into the top-level payload.
        for k, v in record.__dict__.items():
            if k in _BUILTIN_RECORD_FIELDS:
                continue
            if k == "message":
                continue
            # The OTel injected fields are already represented above as
            # trace_id/span_id; don't duplicate them with their raw names.
            if k in {"otelTraceID", "otelSpanID", "otelServiceName", "otelTraceSampled"}:
                continue
            payload.setdefault(k, v)
        # ``record.exc_info`` per stdlib conventions is either None or a
        # 3-tuple ``(type, value, tb)``. The OTel LoggingInstrumentor's
        # patched ``Logger.makeRecord`` can set it to ``True`` instead
        # (an asymmetric stdlib quirk: ``Logger._log`` accepts ``True``
        # and resolves it via ``sys.exc_info()`` BEFORE record creation,
        # but third-party logger wrappers don't always normalize it).
        # Only format when we actually have a tuple — silently skip
        # ``True`` / other truthy non-tuples to avoid crashing emit.
        if isinstance(record.exc_info, tuple) and len(record.exc_info) == 3:
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


def configure_logging(*, level: int = logging.INFO) -> None:
    """Install :class:`StructuredJsonFormatter` on the root + uvicorn loggers.

    Called once at app startup. Idempotent — multiple calls reuse the
    same handler.

    Why: uvicorn ships with its own log config that bypasses any
    formatter installed via ``logging.basicConfig``. Without this
    function, uvicorn's access log emits ``INFO: 1.2.3.4 - "GET /foo
    HTTP/1.1" 200 OK`` plain-text lines to stdout regardless of what
    we attached at the root. This function rebuilds uvicorn's
    handlers so the JSON contract from
    [[09-operations/observability]] § Logs is actually honoured in
    container output (verified missing 2026-05-13 against the live
    Loki datasource).

    The redact filter is attached too so JWT-shaped substrings are
    scrubbed before emit.
    """
    formatter = StructuredJsonFormatter()

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(redact_token_filter)

    # Root logger: replace whatever's there with our single JSON handler.
    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers on repeat calls (idempotency).
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)

    # Uvicorn ships its own logger config that bypasses the root. Each
    # of these loggers has `propagate=False` by default in uvicorn's
    # logging dictConfig, so we must explicitly point them at handlers
    # OR enable propagation. Enabling propagation is simpler and means
    # one handler does all the work.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        # Drop uvicorn's own handlers; let records propagate to root.
        for existing in list(lg.handlers):
            lg.removeHandler(existing)
        lg.propagate = True
        lg.setLevel(level)


__all__ = [
    "StructuredJsonFormatter",
    "configure_logging",
    "redact_token_filter",
    "request_id_var",
]
