"""
Configuration — all env vars, defaults, validation.
"""

import os

from dotenv import load_dotenv

# Load env from the musubi directory, not cwd
_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_dir, ".env"))

# --- Required (validated at call time, not import time, so tests can run without it) ---
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY")

# --- Infrastructure ---
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
BRAIN_PORT = int(os.getenv("BRAIN_PORT", "8100"))

# --- Vector DB ---
MEMORY_COLLECTION = "musubi_memories"
THOUGHT_COLLECTION = "musubi_thoughts"
EMBEDDING_MODEL = "gemini-embedding-001"
VECTOR_SIZE = 3072
DUPLICATE_THRESHOLD = 0.92
