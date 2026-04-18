"""Smoke test: the scaffold imports and the package version is sane."""

import musubi


def test_import_package() -> None:
    assert isinstance(musubi.__version__, str)
    assert musubi.__version__.count(".") == 2


def test_types_subpackage_present() -> None:
    from musubi import types  # noqa: F401
