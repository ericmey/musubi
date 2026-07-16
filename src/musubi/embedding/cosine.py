"""Math utilities for cosine similarity."""


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Compute cosine similarity between two vectors.
    Returns 0.0 if either vector has a zero magnitude."""
    dot = sum(a * b for a, b in zip(v1, v2, strict=True))
    mag1 = sum(a * a for a in v1) ** 0.5
    mag2 = sum(a * a for a in v2) ** 0.5
    if mag1 == 0 or mag2 == 0:
        return 0.0
    return float(dot / (mag1 * mag2))
