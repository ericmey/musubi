"""Result-style wrapper for adapters that prefer typed errors over
exceptions.

Per [[07-interfaces/sdk]] § Result[T, E] pattern. Distinct from
:mod:`musubi.types.common.Result` (the discriminated-union pattern used
internally by planes / lifecycle): this is the SDK-facing shape with
``.is_ok()`` / ``.is_err()`` / ``.ok`` / ``.err`` per the spec
example.
"""

from __future__ import annotations

from dataclasses import dataclass

from musubi.sdk.exceptions import MusubiError


@dataclass(frozen=True)
class SDKResult[T]:
    """Either a successful response (``.ok``) or a typed error
    (``.err``). Mirrors the spec's ``capture_result`` example."""

    ok: T | None = None
    err: MusubiError | None = None

    def is_ok(self) -> bool:
        return self.err is None

    def is_err(self) -> bool:
        return self.err is not None


__all__ = ["SDKResult"]
