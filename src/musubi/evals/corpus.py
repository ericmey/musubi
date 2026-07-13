import hashlib
from pathlib import Path
from typing import Any


def verify_manifest(manifest: dict[str, Any], base_dir: Path) -> bool:
    for fname, expected_hash in manifest.get("files", {}).items():
        fpath = base_dir / fname
        actual = hashlib.sha256(fpath.read_bytes()).hexdigest()
        if actual != expected_hash:
            raise ValueError(f"checksum mismatch for {fname}")
    return True
