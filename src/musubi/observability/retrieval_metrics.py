"""RET-007 — the two bounded retrieval-degradation counters.

Both are registered on the process-wide default registry at import time (the retrieve router imports
this module), so the ``/ops/metrics`` scrape and the contract tests see them. Labels are BOUNDED: the
warning counter is keyed by ``(warning, plane)`` over the fixed code/plane vocabulary, the error
counter by ``kind`` over the four ``RetrievalError`` kinds — no unbounded/free-text label ever enters
Prometheus.
"""

from __future__ import annotations

from musubi.observability.registry import Counter, default_registry

RETRIEVAL_WARNINGS_TOTAL: Counter = default_registry().counter(
    "musubi_retrieval_warnings_total",
    "Count of retrieval degradation warnings, by bounded (warning code, plane).",
    ("warning", "plane"),
)

RETRIEVAL_ERRORS_TOTAL: Counter = default_registry().counter(
    "musubi_retrieval_errors_total",
    "Count of total-failure retrieval requests, by bounded error kind.",
    ("kind",),
)


__all__ = ["RETRIEVAL_ERRORS_TOTAL", "RETRIEVAL_WARNINGS_TOTAL"]
