"""Shared vault-path namespace derivation."""

from pathlib import Path


def infer_namespace(rel_path: str) -> str:
    """Derive the curated namespace encoded by a relative vault path."""
    parts = Path(rel_path).parts
    if len(parts) >= 2:
        tenant = parts[0]
        presence = parts[1]
        return f"{tenant}/{presence}/curated"
    return "system/internal/curated"
