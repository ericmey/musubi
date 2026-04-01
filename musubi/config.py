"""
Configuration — all env vars, defaults, validation.
"""

import os

from dotenv import load_dotenv

# Load env from the musubi directory, not cwd
_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_dir, ".env"))

# --- Required ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is required. Set it in .env or as an environment variable.")

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
