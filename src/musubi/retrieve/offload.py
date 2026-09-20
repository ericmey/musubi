"""Dedicated capacity control for synchronous Qdrant retrieval work."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial

QDRANT_OFFLOAD_WORKERS = 16
"""Maximum blocking Qdrant retrieval calls active in one API process."""

_QDRANT_EXECUTOR = ThreadPoolExecutor(
    max_workers=QDRANT_OFFLOAD_WORKERS,
    thread_name_prefix="musubi-qdrant-retrieve",
)


async def run_qdrant_offload[T](
    function: Callable[..., T], /, *args: object, **kwargs: object
) -> T:
    """Run blocking retrieval I/O on the dedicated, deliberately sized executor."""
    loop = asyncio.get_running_loop()
    call = partial(function, *args, **kwargs)
    return await loop.run_in_executor(_QDRANT_EXECUTOR, call)


__all__ = ["QDRANT_OFFLOAD_WORKERS", "run_qdrant_offload"]
