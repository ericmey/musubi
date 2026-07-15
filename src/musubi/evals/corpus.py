import hashlib
from pathlib import Path
from typing import Any


def verify_manifest(manifest: dict[str, Any], base_dir: Path) -> bool:
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("manifest files must be a non-empty mapping")
    for fname, expected_hash in files.items():
        if not isinstance(fname, str) or not isinstance(expected_hash, str):
            raise ValueError("manifest file names and checksums must be strings")
        fpath = base_dir / fname
        try:
            content = fpath.read_bytes()
        except OSError as exc:
            raise ValueError(f"manifest file unavailable: {fname}") from exc
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected_hash:
            raise ValueError(f"checksum mismatch for {fname}")
    return True
