"""Shared pytest fixtures.

Fixtures are added as slices land. Reference: vault's ``_slices/test-fixtures.md``.
This file starts empty so ``pytest`` runs clean against the scaffold.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest


@pytest.fixture
def sample_namespace() -> str:
    """A well-formed ``{tenant}/{presence}/{plane}`` namespace for tests."""
    return "eric/claude-code/episodic"


class _FakeWordTokenizer:
    """Deterministic, offline stand-in for the BGE-M3 / SPLADE-v3 tokenizers.

    One token per whitespace-delimited word; offsets map back to the original
    character spans so the chunkers' window / overlap / offset logic all work.
    Mirrors the injectable fakes already used in ``test_chunked_embedder`` and
    ``test_artifact`` — the difference is the autouse fixture below installs it
    for *every* test, so no test can silently fall through to a live tokenizer
    fetch.
    """

    def encode(self, sequence: str) -> Any:
        import re

        spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", sequence)]

        class _Encoding:
            def __init__(self, s: list[tuple[int, int]]) -> None:
                self.ids = list(range(len(s)))
                self.offsets = s

        return _Encoding(spans)


@pytest.fixture(autouse=True)
def _offline_tokenizers(request: pytest.FixtureRequest) -> Iterator[None]:
    """Keep the unit suite offline w.r.t. HuggingFace tokenizers.

    Chunker / artifact-plane tests that don't inject their own tokenizer fall
    through to ``Tokenizer.from_pretrained('BAAI/bge-m3' | 'naver/splade-v3')``
    — a live network call HuggingFace rate-limits with HTTP 429, which flakes
    CI (and pulls a gated repo). Per the project's stated test philosophy, the
    real tokenizers are exercised only by the Docker build's pre-cache step and
    the live deploy; unit tests use this deterministic fake.

    Patches all three bind sites (the chunking module defines both loaders;
    ``embedding.chunked`` imports ``load_splade_v3_tokenizer`` by name, so its
    own reference must be patched too). Uses ``mock.patch`` rather than the
    ``monkeypatch`` fixture deliberately: as an autouse fixture, depending on
    ``monkeypatch`` would reorder its finalizer relative to other fixtures'
    teardown (it broke ``test_tracing``'s ``__import__``-faking teardown). A
    self-contained ``ExitStack`` keeps this fixture's lifecycle independent.
    Opt out with ``@pytest.mark.real_tokenizers`` for a test that genuinely
    needs the real vocab — none in the default suite (live-model tests are
    skip-marked).
    """
    if request.node.get_closest_marker("real_tokenizers"):
        yield
        return
    fake = _FakeWordTokenizer()
    targets = (
        "musubi.planes.artifact.chunking.load_bge_m3_tokenizer",
        "musubi.planes.artifact.chunking.load_splade_v3_tokenizer",
        "musubi.embedding.chunked.load_splade_v3_tokenizer",
    )
    with contextlib.ExitStack() as stack:
        for target in targets:
            stack.enter_context(patch(target, lambda: fake))
        yield


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "gpu: mark test to run only when MUSUBI_GPU_AVAILABLE=1 is set"
    )
    config.addinivalue_line(
        "markers",
        "real_tokenizers: opt out of the offline fake-tokenizer autouse fixture "
        "(use the real BGE-M3 / SPLADE-v3 tokenizers)",
    )


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("gpu"):
        import os

        if os.environ.get("MUSUBI_GPU_AVAILABLE") != "1":
            pytest.skip("Test requires MUSUBI_GPU_AVAILABLE=1")


@pytest.fixture
def require_gpu() -> None:
    import os

    if os.environ.get("MUSUBI_GPU_AVAILABLE") != "1":
        pytest.skip("Test requires MUSUBI_GPU_AVAILABLE=1")
