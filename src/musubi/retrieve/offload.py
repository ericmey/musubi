"""Dedicated capacity control for synchronous Qdrant retrieval work."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial

QDRANT_REQUIRED_OFFLOAD_WORKERS = 8
"""Capacity reserved for required query and authoritative-resolution work."""

QDRANT_OPTIONAL_OFFLOAD_WORKERS = 8
"""Capacity reserved for optional lineage hydration work."""

QDRANT_OFFLOAD_WORKERS = QDRANT_REQUIRED_OFFLOAD_WORKERS + QDRANT_OPTIONAL_OFFLOAD_WORKERS
"""Total blocking Qdrant retrieval calls active in one API process."""

_QDRANT_REQUIRED_EXECUTOR = ThreadPoolExecutor(
    max_workers=QDRANT_REQUIRED_OFFLOAD_WORKERS,
    thread_name_prefix="musubi-qdrant-required",
)
_QDRANT_OPTIONAL_EXECUTOR = ThreadPoolExecutor(
    max_workers=QDRANT_OPTIONAL_OFFLOAD_WORKERS,
    thread_name_prefix="musubi-qdrant-optional",
)


async def run_qdrant_offload[T](
    function: Callable[..., T], /, *args: object, **kwargs: object
) -> T:
    """Run blocking retrieval I/O on the dedicated, deliberately sized executor."""
    loop = asyncio.get_running_loop()
    call = partial(function, *args, **kwargs)
    context = copy_context()
    return await loop.run_in_executor(_QDRANT_REQUIRED_EXECUTOR, context.run, call)


async def run_optional_qdrant_offload[T](
    function: Callable[..., T], /, *args: object, **kwargs: object
) -> T:
    """Run optional lineage I/O without consuming capacity required for base retrieval."""
    loop = asyncio.get_running_loop()
    call = partial(function, *args, **kwargs)
    context = copy_context()
    return await loop.run_in_executor(_QDRANT_OPTIONAL_EXECUTOR, context.run, call)


__all__ = [
    "QDRANT_OFFLOAD_WORKERS",
    "QDRANT_OPTIONAL_OFFLOAD_WORKERS",
    "QDRANT_REQUIRED_OFFLOAD_WORKERS",
    "run_optional_qdrant_offload",
    "run_qdrant_offload",
]
