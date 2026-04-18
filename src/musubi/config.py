"""Single source of truth for Musubi configuration.

Importers:

- Call :func:`get_settings` — never re-instantiate :class:`Settings`.
- Never read ``os.environ`` elsewhere. The guardrails (and
  ``tests/test_config.py::test_no_module_imports_os_environ_for_config``)
  flag regressions.

The accessor is ``lru_cache``-backed so settings load exactly once per process
and every importer shares one instance. Tests clear the cache with
``get_settings.cache_clear()``.
"""

from __future__ import annotations

import os
from functools import lru_cache

from musubi.settings import Settings


def _dotenv_path() -> str:
    """Location of the ``.env`` file pydantic-settings should parse.

    Reading ``MUSUBI_DOTENV`` here is the *one* allowed ``os.environ`` use
    outside of pydantic-settings itself: it only controls which file the
    settings loader looks at, and exists so tests can point at a tmp-path
    ``.env`` without polluting the operator's shell.
    """
    return os.environ.get("MUSUBI_DOTENV", ".env")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` instance.

    First call parses env + ``.env`` and validates. Subsequent calls return
    the cached instance. Tests use ``get_settings.cache_clear()`` to force a
    reload after mutating the environment.
    """
    return Settings(_env_file=_dotenv_path())  # type: ignore[call-arg]


__all__ = ["Settings", "get_settings"]
