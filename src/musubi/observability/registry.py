"""Minimal in-process Prometheus metrics registry.

Hand-rolled (no `prometheus-client` dep — keeps the slice scope tight,
avoids an ADR for a 12-deep transitive tree). Three instrument types:

- :class:`Counter` — monotonic; ``inc(n=1)``.
- :class:`Histogram` — observe(value); buckets render the standard
  `_bucket{le=...}` + `_sum` + `_count` triple.
- :class:`Gauge` — ``set`` / ``inc`` / ``dec``.

:class:`Registry` is the collection. :func:`render_text_format` walks
it and emits Prometheus 0.0.4 exposition format.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


def _format_label_value(v: Any) -> str:
    s = str(v)
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labelnames: tuple[str, ...], values: tuple[str, ...]) -> str:
    if not labelnames:
        return ""
    parts = [f'{n}="{_format_label_value(v)}"' for n, v in zip(labelnames, values, strict=True)]
    return "{" + ",".join(parts) + "}"


def _format_number(x: float) -> str:
    """Trim trailing .0 so integer counters render cleanly."""
    if x == int(x):
        return str(int(x))
    return repr(x)


class _LabelsDict:
    """A dict keyed by label-value tuple, returning a per-key counter
    or histogram or gauge instance. Thread-safe via the parent
    instrument's lock."""

    def __init__(self, factory: Any, lock: threading.Lock) -> None:
        self._factory = factory
        self._lock = lock
        self._values: dict[tuple[str, ...], Any] = {}

    def get(self, key: tuple[str, ...]) -> Any:
        with self._lock:
            inst = self._values.get(key)
            if inst is None:
                inst = self._factory()
                self._values[key] = inst
            return inst

    def items(self) -> Iterable[tuple[tuple[str, ...], Any]]:
        with self._lock:
            return list(self._values.items())


class Counter:
    """Monotonic counter."""

    type_name = "counter"

    def __init__(self, name: str, help_text: str, labelnames: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.labelnames = labelnames
        self._lock = threading.Lock()
        self._value = 0.0
        self._labelled = _LabelsDict(lambda: _CounterInstance(), self._lock) if labelnames else None

    def inc(self, amount: float = 1.0) -> None:
        if self.labelnames:
            raise ValueError(f"counter {self.name!r} requires .labels(...)")
        with self._lock:
            self._value += amount

    def labels(self, **kwargs: Any) -> _CounterInstance:
        if not self.labelnames:
            raise ValueError(f"counter {self.name!r} has no labels")
        if set(kwargs.keys()) != set(self.labelnames):
            raise ValueError(
                f"counter {self.name!r} expects labels {self.labelnames}, got {tuple(kwargs)}"
            )
        key = tuple(str(kwargs[n]) for n in self.labelnames)
        assert self._labelled is not None
        inst: _CounterInstance = self._labelled.get(key)
        return inst

    def collect(self) -> Iterable[tuple[tuple[str, ...], float]]:
        if self.labelnames:
            assert self._labelled is not None
            for key, inst in self._labelled.items():
                yield key, inst.value
        else:
            yield (), self._value


class _CounterInstance:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0.0

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value += amount

    @property
    def value(self) -> float:
        return self._value


@dataclass
class _HistogramInstance:
    buckets: tuple[float, ...]
    bucket_counts: list[int] = field(default_factory=list)
    count: int = 0
    sum: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.bucket_counts:
            self.bucket_counts = [0 for _ in self.buckets]

    def observe(self, value: float) -> None:
        v = max(0.0, value)
        with self._lock:
            self.count += 1
            self.sum += v
            for i, upper in enumerate(self.buckets):
                if v <= upper:
                    self.bucket_counts[i] += 1


class Histogram:
    """Histogram with caller-defined buckets. ``+Inf`` is appended."""

    type_name = "histogram"
    _DEFAULT_BUCKETS: tuple[float, ...] = (
        5.0,
        10.0,
        25.0,
        50.0,
        100.0,
        250.0,
        500.0,
        1000.0,
        2500.0,
        5000.0,
    )

    def __init__(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...] = (),
        buckets: tuple[float, ...] | None = None,
    ) -> None:
        self.name = name
        self.help_text = help_text
        self.labelnames = labelnames
        self.buckets = tuple(buckets) if buckets is not None else self._DEFAULT_BUCKETS
        self._lock = threading.Lock()
        self._unlabelled = _HistogramInstance(buckets=self.buckets) if not labelnames else None
        self._labelled = (
            _LabelsDict(lambda: _HistogramInstance(buckets=self.buckets), self._lock)
            if labelnames
            else None
        )

    def observe(self, value: float) -> None:
        if self.labelnames:
            raise ValueError(f"histogram {self.name!r} requires .labels(...)")
        assert self._unlabelled is not None
        self._unlabelled.observe(value)

    def labels(self, **kwargs: Any) -> _HistogramInstance:
        if not self.labelnames:
            raise ValueError(f"histogram {self.name!r} has no labels")
        if set(kwargs.keys()) != set(self.labelnames):
            raise ValueError(
                f"histogram {self.name!r} expects labels {self.labelnames}, got {tuple(kwargs)}"
            )
        key = tuple(str(kwargs[n]) for n in self.labelnames)
        assert self._labelled is not None
        inst: _HistogramInstance = self._labelled.get(key)
        return inst

    def collect(self) -> Iterable[tuple[tuple[str, ...], _HistogramInstance]]:
        if self.labelnames:
            assert self._labelled is not None
            yield from self._labelled.items()
        else:
            assert self._unlabelled is not None
            yield (), self._unlabelled


class Gauge:
    """Gauge — set / inc / dec arbitrary float."""

    type_name = "gauge"

    def __init__(self, name: str, help_text: str, labelnames: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.labelnames = labelnames
        self._lock = threading.Lock()
        self._value = 0.0

    def set(self, value: float) -> None:
        with self._lock:
            self._value = value

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value += amount

    def dec(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value -= amount

    def collect(self) -> Iterable[tuple[tuple[str, ...], float]]:
        yield (), self._value


_Instrument = Counter | Histogram | Gauge


class Registry:
    """Holds metric instruments + renders them in Prometheus text format."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[str, _Instrument] = {}

    def counter(
        self, name: str, help_text: str, labelnames: tuple[str, ...] | list[str] = ()
    ) -> Counter:
        with self._lock:
            existing = self._metrics.get(name)
            if existing is not None:
                if not isinstance(existing, Counter):
                    raise ValueError(f"{name!r} already registered as {type(existing).__name__}")
                return existing
            c = Counter(name, help_text, tuple(labelnames))
            self._metrics[name] = c
            return c

    def histogram(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...] | list[str] = (),
        buckets: tuple[float, ...] | None = None,
    ) -> Histogram:
        with self._lock:
            existing = self._metrics.get(name)
            if existing is not None:
                if not isinstance(existing, Histogram):
                    raise ValueError(f"{name!r} already registered as {type(existing).__name__}")
                return existing
            h = Histogram(name, help_text, tuple(labelnames), buckets)
            self._metrics[name] = h
            return h

    def gauge(
        self, name: str, help_text: str, labelnames: tuple[str, ...] | list[str] = ()
    ) -> Gauge:
        with self._lock:
            existing = self._metrics.get(name)
            if existing is not None:
                if not isinstance(existing, Gauge):
                    raise ValueError(f"{name!r} already registered as {type(existing).__name__}")
                return existing
            g = Gauge(name, help_text, tuple(labelnames))
            self._metrics[name] = g
            return g

    def _instruments(self) -> list[_Instrument]:
        with self._lock:
            return list(self._metrics.values())


def render_text_format(registry: Registry) -> str:
    """Render the registry in Prometheus exposition format 0.0.4."""
    lines: list[str] = []
    for inst in registry._instruments():
        lines.append(f"# HELP {inst.name} {inst.help_text}")
        lines.append(f"# TYPE {inst.name} {inst.type_name}")
        if isinstance(inst, Counter | Gauge):
            for labels_tuple, value in inst.collect():
                ls = _format_labels(inst.labelnames, labels_tuple)
                lines.append(f"{inst.name}{ls} {_format_number(value)}")
        elif isinstance(inst, Histogram):
            for labels_tuple, hist in inst.collect():
                # `observe` already increments every bucket where v <= upper,
                # so bucket_counts are cumulative-by-construction. Render
                # each bucket's count verbatim.
                base_labels = tuple(zip(inst.labelnames, labels_tuple, strict=True))
                for upper, count_at in zip(inst.buckets, hist.bucket_counts, strict=True):
                    bucket_labels = (*base_labels, ("le", _format_number(upper)))
                    bls = (
                        "{"
                        + ",".join(f'{k}="{_format_label_value(v)}"' for k, v in bucket_labels)
                        + "}"
                    )
                    lines.append(f"{inst.name}_bucket{bls} {count_at}")
                # +Inf bucket = total count
                inf_labels = (*base_labels, ("le", "+Inf"))
                ibls = (
                    "{" + ",".join(f'{k}="{_format_label_value(v)}"' for k, v in inf_labels) + "}"
                )
                lines.append(f"{inst.name}_bucket{ibls} {hist.count}")
                ls = _format_labels(inst.labelnames, labels_tuple)
                lines.append(f"{inst.name}_count{ls} {hist.count}")
                lines.append(f"{inst.name}_sum{ls} {_format_number(hist.sum)}")
    return "\n".join(lines) + ("\n" if lines else "")


_DEFAULT_REGISTRY: Registry | None = None
_DEFAULT_REGISTRY_LOCK = threading.Lock()


def default_registry() -> Registry:
    """Process-wide singleton registry. The API + workers share it."""
    global _DEFAULT_REGISTRY
    with _DEFAULT_REGISTRY_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = Registry()
        return _DEFAULT_REGISTRY


__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "Registry",
    "default_registry",
    "render_text_format",
]
