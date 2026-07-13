"""D5 spike — multipart file-size bypass, spool/rewind, ingress byte cap, canonical identity.

Yua REV2 GO (2026-07-12T23:44). Design-only, ZERO src, isolated worktree branch from Phase A
a1c916e (PR403 untouched). Proves, against the pinned starlette 1.0.0 parser + real
SpooledTemporaryFile/UploadFile:

  1. FILE parts BYPASS max_part_size (formparsers.py:160-167 `if file is None`) — a >cap file is
     accepted and spooled; a >cap NON-file field is rejected. So memory is bounded by the 1MB
     spool but total file/disk is UNBOUNDED — the DoS is real and the primary control must be an
     ingress byte cap, not a digest-time check.
  2. spool rollover + rewind: a small file stays in memory, a large one rolls to disk; seek(0)
     rewinds and returns identical full bytes in BOTH modes.
  3. proposed pure-ASGI INGRESS BYTE CAP: counts ACTUAL received bytes and rejects before the
     parser — independent of Content-Length (missing / false) and of chunking.
  4. digest backstop + rewind: chunked SHA-256 with a per-route byte cap; seek(0); handler read
     == original; digest == one-shot SHA.
  5. domain-separated, length-prefixed canonical identity: collision-resistant and boundary
     unambiguous.

    UV_PROJECT_ENVIRONMENT=/Users/ericmey/Projects/musubi/.venv uv run --no-sync \
      pytest tests/api/spikes/test_d5_multipart_digest_ingress.py -v
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from tempfile import SpooledTemporaryFile
from typing import Any

import pytest
from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartParser

BOUNDARY = "----d5boundary"


# --------------------------------------------------------------------------- #
# 1. FILE parts bypass max_part_size; NON-file parts do not
# --------------------------------------------------------------------------- #


def _multipart_body(*, field: tuple[str, bytes] | None, file: tuple[str, bytes] | None) -> bytes:
    lines: list[bytes] = []
    if field is not None:
        name, value = field
        lines += [
            f"--{BOUNDARY}".encode(),
            f'Content-Disposition: form-data; name="{name}"'.encode(),
            b"",
            value,
        ]
    if file is not None:
        name, value = file
        lines += [
            f"--{BOUNDARY}".encode(),
            f'Content-Disposition: form-data; name="{name}"; filename="{name}.bin"'.encode(),
            b"Content-Type: application/octet-stream",
            b"",
            value,
        ]
    lines += [f"--{BOUNDARY}--".encode(), b""]
    return b"\r\n".join(lines)


def _headers() -> Headers:
    return Headers({"content-type": f"multipart/form-data; boundary={BOUNDARY}"})


async def _stream(body: bytes, chunk: int = 4096) -> AsyncGenerator[bytes, None]:
    for i in range(0, len(body), chunk):
        yield body[i : i + chunk]


async def _parse(body: bytes, *, max_part_size: int) -> list[tuple[str, str | UploadFile]]:
    parser = MultiPartParser(_headers(), _stream(body), max_part_size=max_part_size)
    form = await parser.parse()
    return list(form.multi_items())


def test_file_part_bypasses_max_part_size() -> None:
    """A FILE part larger than max_part_size is ACCEPTED (bypasses the check → spool)."""
    big = b"F" * 500
    items = asyncio.run(_parse(_multipart_body(field=None, file=("f", big)), max_part_size=100))
    assert len(items) == 1
    _name, value = items[0]
    assert isinstance(value, UploadFile), "the >cap file part must be accepted, not rejected"
    assert asyncio.run(value.read()) == big, "the full oversized file is available (spooled)"


def test_nonfile_field_over_max_part_size_is_rejected() -> None:
    """A NON-file field larger than max_part_size IS rejected — proving the check is field-only."""
    from starlette.formparsers import MultiPartException

    big = b"V" * 500
    with pytest.raises(MultiPartException, match="maximum size"):
        asyncio.run(_parse(_multipart_body(field=("x", big), file=None), max_part_size=100))


# --------------------------------------------------------------------------- #
# 2. spool rollover + rewind (in-memory AND disk)
# --------------------------------------------------------------------------- #


async def _digest_and_rewind(uf: UploadFile, *, chunk: int = 256) -> tuple[bytes, int]:
    """Chunked SHA-256 of the file, then seek(0) rewind. Returns (digest, total_bytes)."""
    h = hashlib.sha256()
    total = 0
    while data := await uf.read(chunk):
        h.update(data)
        total += len(data)
    await uf.seek(0)  # rewind so the handler still gets the full original file
    return h.digest(), total


def test_spool_rollover_and_rewind_both_modes() -> None:
    for size, expect_in_memory in [(100, True), (5000, False)]:  # spool_max_size = 1000
        # SIM115: the spool is owned by UploadFile and read across multiple awaits — a
        # with-block would close it prematurely.
        spool: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(max_size=1000)  # noqa: SIM115
        data = bytes((i % 251) for i in range(size))
        spool.write(data)
        spool.seek(0)
        uf = UploadFile(file=spool, size=size, filename="x.bin")  # type: ignore[arg-type]
        assert uf._in_memory is expect_in_memory, f"size={size} in_memory expectation"
        digest, total = asyncio.run(_digest_and_rewind(uf))
        assert total == size
        assert digest == hashlib.sha256(data).digest(), "chunked digest == one-shot SHA"
        # rewound: the handler read after digest returns the FULL original bytes
        assert asyncio.run(uf.read()) == data, (
            f"seek(0) rewind must return full bytes (size={size})"
        )


# --------------------------------------------------------------------------- #
# 3. proposed pure-ASGI ingress byte cap — independent of Content-Length/chunking
# --------------------------------------------------------------------------- #

Message = dict[str, Any]
Scope = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class _PayloadTooLarge(Exception):
    def __init__(self, seen: int) -> None:
        super().__init__(f"ingress body exceeded cap at {seen} bytes")
        self.seen = seen


class IngressByteCap:
    """Counts ACTUAL http.request body bytes and rejects before the parser/handler. Never trusts
    Content-Length; accumulates across chunked events."""

    def __init__(
        self, app: Callable[[Scope, Receive, Send], Awaitable[None]], *, max_bytes: int
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        seen = 0

        async def capped_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _PayloadTooLarge(seen)  # in prod: send 413 + stop; spike raises to assert
            return message

        await self.app(scope, capped_receive, send)


def _body_consumer() -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    """A downstream ASGI app that drains the whole request body (as the parser would)."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        more = True
        while more:
            m = await receive()
            more = m.get("more_body", False)

    return app


def _receive_from(chunks: list[bytes]) -> Receive:
    idx = {"i": 0}

    async def receive() -> Message:
        i = idx["i"]
        idx["i"] += 1
        return {"type": "http.request", "body": chunks[i], "more_body": i < len(chunks) - 1}

    return receive


def test_ingress_cap_rejects_regardless_of_content_length() -> None:
    # FALSE Content-Length (claims tiny) but the actual body is large, sent in chunks.
    scope: Scope = {"type": "http", "headers": [(b"content-length", b"5")]}
    chunks = [b"A" * 400, b"B" * 400]  # 800 actual bytes, cap 500
    app = IngressByteCap(_body_consumer(), max_bytes=500)
    with pytest.raises(_PayloadTooLarge) as ei:
        asyncio.run(_run_asgi(app, scope, _receive_from(chunks)))
    assert ei.value.seen > 500, "cap fires on ACTUAL bytes, not the (false/small) Content-Length"


def test_ingress_cap_allows_within_limit_missing_content_length() -> None:
    # No Content-Length header at all, streamed — a within-limit body passes.
    scope: Scope = {"type": "http", "headers": []}
    chunks = [b"x" * 100, b"y" * 100]  # 200 bytes, cap 500
    app = IngressByteCap(_body_consumer(), max_bytes=500)
    asyncio.run(_run_asgi(app, scope, _receive_from(chunks)))  # must not raise


async def _run_asgi(
    app: Callable[[Scope, Receive, Send], Awaitable[None]], scope: Scope, receive: Receive
) -> None:
    async def send(_m: Message) -> None:
        return None

    await app(scope, receive, send)


# --------------------------------------------------------------------------- #
# 5. domain-separated, length-prefixed canonical identity
# --------------------------------------------------------------------------- #

_DOMAIN = b"musubi-idem-multipart-v1"


def _canonical_identity(fields: dict[str, str], file_sha: bytes) -> bytes:
    parts: list[bytes] = [_DOMAIN]
    for k in sorted(fields):  # canonical field order
        kb, vb = k.encode(), fields[k].encode()
        parts.append(len(kb).to_bytes(4, "big") + kb)
        parts.append(len(vb).to_bytes(4, "big") + vb)
    parts.append(len(file_sha).to_bytes(4, "big") + file_sha)
    return hashlib.sha256(b"".join(parts)).digest()


def test_identity_collision_resistance() -> None:
    sha_a = hashlib.sha256(b"file-A").digest()
    sha_b = hashlib.sha256(b"file-B").digest()
    base = {"namespace": "eric/claude-code/artifact", "title": "doc"}
    # same fields, different file → different identity
    assert _canonical_identity(base, sha_a) != _canonical_identity(base, sha_b)
    # same file, different fields → different identity
    assert _canonical_identity(base, sha_a) != _canonical_identity({**base, "title": "doc2"}, sha_a)


def test_identity_length_prefix_prevents_boundary_ambiguity() -> None:
    """Without length-prefixing, {"a":"bc","d":"e"} and {"a":"b","cd":"e"} could serialise to the
    same byte-run. Length prefixes make them distinct."""
    sha = hashlib.sha256(b"same-file").digest()
    id1 = _canonical_identity({"a": "bc", "d": "e"}, sha)
    id2 = _canonical_identity({"a": "b", "cd": "e"}, sha)
    assert id1 != id2, "length-prefixed canonicalisation must disambiguate field boundaries"
