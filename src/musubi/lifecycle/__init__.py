"""Lifecycle engine — canonical transition function + APScheduler worker.

Two public entry points:

- :func:`musubi.lifecycle.transitions.transition` — the only code path that
  mutates ``state`` on a ``MusubiObject``. Every call emits exactly one
  :class:`~musubi.types.lifecycle_event.LifecycleEvent` (see
  [[04-data-model/lifecycle#No silent mutation]]).
- :class:`musubi.lifecycle.scheduler.build_scheduler` — wires per-job triggers
  onto APScheduler's ``BlockingScheduler`` with an on-disk sqlite job store
  (see [[06-ingestion/lifecycle-engine]]).

The per-job sweep functions (maturation, synthesis, promotion, demotion,
reflection, reconcile) are owned by the downstream lifecycle slices. This
module owns the *scaffolding* — the transition primitive, the event sink,
and the scheduler that dispatches those sweeps.
"""

from musubi.lifecycle.events import LifecycleEventSink
from musubi.lifecycle.scheduler import (
    Job,
    JobFailureMetrics,
    NamespaceLock,
    TestingScheduler,
    build_default_jobs,
    build_scheduler,
    file_lock,
)
from musubi.lifecycle.transitions import (
    LineageUpdates,
    TransitionError,
    TransitionResult,
    transition,
)

__all__ = [
    "Job",
    "JobFailureMetrics",
    "LifecycleEventSink",
    "LineageUpdates",
    "NamespaceLock",
    "TestingScheduler",
    "TransitionError",
    "TransitionResult",
    "build_default_jobs",
    "build_scheduler",
    "file_lock",
    "transition",
]
