"""Exact-byte, stored-unindexed artifact escrow for episodic retraction.

The blob is the first durable boundary.  Publication is temp-file + fsync +
hard-link so the deterministic final address is never clobbered and never
appears partially written.  A head becomes readable only after exact final
readback succeeds.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ksuid import Ksuid

from musubi.types.artifact import SourceArtifact
from musubi.types.common import Err, Ok, Result, validate_ksuid, validate_namespace

_DOMAIN = b"musubi-retraction-escrow-v1"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class EscrowAddress:
    """Deterministic sibling-artifact address and synthetic public metadata."""

    artifact_namespace: str
    artifact_id: str
    title: str
    filename: str


@dataclass(frozen=True)
class ArtifactEscrowError:
    """Fail-closed escrow result which identifies an address, never its content."""

    code: str
    detail: str
    artifact_namespace: str
    artifact_id: str


class StoredUnindexedIndexingError(RuntimeError):
    """A live stored-unindexed head reached an indexing entry point."""


class _ArtifactHeadStore(Protocol):
    async def create(self, artifact: SourceArtifact) -> SourceArtifact: ...

    async def get(self, *, namespace: str, object_id: str) -> SourceArtifact | None: ...


def derive_escrow_address(
    *,
    source_namespace: str,
    source_object_id: str,
    original_sha256: str,
) -> EscrowAddress:
    """Derive ADR 0042's versioned, domain-separated escrow address."""
    validate_namespace(source_namespace)
    parts = source_namespace.split("/")
    if len(parts) != 3 or parts[2] != "episodic":
        raise ValueError("retraction escrow source namespace must be episodic")
    validate_ksuid(source_object_id)
    if not _DIGEST_RE.fullmatch(original_sha256):
        raise ValueError("original_sha256 must be 64 lowercase hexadecimal characters")

    artifact_namespace = f"{parts[0]}/{parts[1]}/artifact"
    validate_namespace(artifact_namespace)
    source_raw = bytes(Ksuid.from_base62(source_object_id))
    digest_raw = bytes.fromhex(original_sha256)
    payload = hashlib.sha256(
        _DOMAIN
        + b"\x00"
        + source_namespace.encode("utf-8")
        + b"\x00"
        + source_object_id.encode("ascii")
        + b"\x00"
        + digest_raw
    ).digest()[:16]
    artifact_id = str(Ksuid.from_bytes(source_raw[:4] + payload))
    title = f"retracted-original-{source_object_id}-{original_sha256[:12]}"
    return EscrowAddress(
        artifact_namespace=artifact_namespace,
        artifact_id=artifact_id,
        title=title,
        filename=f"{title}.txt",
    )


class ArtifactEscrowWriter:
    """Publish or exact-readback-reuse one deterministic escrow blob and head."""

    def __init__(self, *, plane: _ArtifactHeadStore, blob_root: Path | str) -> None:
        self._plane = plane
        self._blob_root = Path(blob_root)

    async def store(
        self,
        *,
        source_namespace: str,
        source_object_id: str,
        original: bytes,
    ) -> Result[SourceArtifact, ArtifactEscrowError]:
        original_sha256 = hashlib.sha256(original).hexdigest()
        address = derive_escrow_address(
            source_namespace=source_namespace,
            source_object_id=source_object_id,
            original_sha256=original_sha256,
        )
        final = self._blob_root / address.artifact_namespace / address.artifact_id
        existed_before = final.exists()

        if not existed_before:
            publish_error = self._publish_blob(final=final, original=original, address=address)
            if publish_error is not None:
                return Err(error=publish_error)

        readback_error = self._verify_blob(
            final=final,
            expected_sha256=original_sha256,
            expected_size=len(original),
            address=address,
            existing=existed_before,
        )
        if readback_error is not None:
            return Err(error=readback_error)

        expected = SourceArtifact(
            object_id=address.artifact_id,
            namespace=address.artifact_namespace,
            title=address.title,
            filename=address.filename,
            sha256=original_sha256,
            content_type="text/plain; charset=utf-8",
            size_bytes=len(original),
            chunker="stored-unindexed-v1",
            artifact_state="stored_unindexed",
            publication_version=0,
            ingestion_metadata={
                "kind": "retraction_escrow_v1",
                "source_namespace": source_namespace,
                "source_object_id": source_object_id,
            },
        )
        existing_head = await self._plane.get(
            namespace=address.artifact_namespace,
            object_id=address.artifact_id,
        )
        if existing_head is not None:
            return self._adopt_or_reject_head(existing_head, expected, address)

        try:
            await self._plane.create(expected)
        except Exception:
            # A concurrent identical creator may have won between get and create.
            landed = await self._plane.get(
                namespace=address.artifact_namespace,
                object_id=address.artifact_id,
            )
            if landed is not None and self._heads_match(landed, expected):
                return Ok(value=landed)
            return Err(
                error=self._error(
                    "head_publish_failed",
                    "verified escrow blob exists but its artifact head did not publish",
                    address,
                )
            )

        landed = await self._plane.get(
            namespace=address.artifact_namespace,
            object_id=address.artifact_id,
        )
        if landed is None or not self._heads_match(landed, expected):
            return Err(
                error=self._error(
                    "head_readback_mismatch",
                    "published escrow head failed exact metadata readback",
                    address,
                )
            )
        return Ok(value=landed)

    def _publish_blob(
        self, *, final: Path, original: bytes, address: EscrowAddress
    ) -> ArtifactEscrowError | None:
        final.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            fd, temp_name = tempfile.mkstemp(
                dir=final.parent,
                prefix=f".{address.artifact_id}.",
                suffix=".tmp",
            )
            temp_path = Path(temp_name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            # A concurrent winner may have published the same deterministic
            # address between the existence check and this no-clobber link.
            with suppress(FileExistsError):
                os.link(temp_path, final)
            self._fsync_directory(final.parent)
            return None
        except OSError:
            return self._error(
                "blob_publish_failed",
                "escrow blob did not reach its deterministic final address",
                address,
            )
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def _verify_blob(
        cls,
        *,
        final: Path,
        expected_sha256: str,
        expected_size: int,
        address: EscrowAddress,
        existing: bool,
    ) -> ArtifactEscrowError | None:
        try:
            actual = final.read_bytes()
        except OSError:
            return cls._error(
                "blob_mismatch" if existing else "blob_readback_mismatch",
                "escrow blob could not be read back exactly",
                address,
            )
        if len(actual) != expected_size or hashlib.sha256(actual).hexdigest() != expected_sha256:
            return cls._error(
                "blob_mismatch" if existing else "blob_readback_mismatch",
                "escrow blob length or digest did not match the authorized original",
                address,
            )
        return None

    @classmethod
    def _adopt_or_reject_head(
        cls,
        actual: SourceArtifact,
        expected: SourceArtifact,
        address: EscrowAddress,
    ) -> Result[SourceArtifact, ArtifactEscrowError]:
        if cls._heads_match(actual, expected):
            return Ok(value=actual)
        return Err(
            error=cls._error(
                "head_mismatch",
                "deterministic escrow address contains a divergent artifact head",
                address,
            )
        )

    @staticmethod
    def _heads_match(actual: SourceArtifact, expected: SourceArtifact) -> bool:
        fields = (
            "object_id",
            "namespace",
            "title",
            "filename",
            "sha256",
            "content_type",
            "size_bytes",
            "chunker",
            "artifact_state",
            "chunk_count",
            "committed_generation",
            "committed_owner",
            "index_operation_id",
            "failure_reason",
            "publication_version",
            "ingestion_metadata",
        )
        return all(getattr(actual, field) == getattr(expected, field) for field in fields)

    @staticmethod
    def _error(code: str, detail: str, address: EscrowAddress) -> ArtifactEscrowError:
        return ArtifactEscrowError(
            code=code,
            detail=detail,
            artifact_namespace=address.artifact_namespace,
            artifact_id=address.artifact_id,
        )


__all__ = [
    "ArtifactEscrowError",
    "ArtifactEscrowWriter",
    "EscrowAddress",
    "StoredUnindexedIndexingError",
    "derive_escrow_address",
]
