"""
Gemini embedding with retry logic and error handling.
"""

import logging
import time

from google import genai

from .config import GEMINI_API_KEY, EMBEDDING_MODEL

logger = logging.getLogger(__name__)

_client = genai.Client(api_key=GEMINI_API_KEY)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds, doubles each retry


def embed_text(text: str) -> list[float]:
    """
    Embed text using Gemini embedding-001.

    Retries up to 3 times with exponential backoff on transient failures.
    Raises RuntimeError if all attempts fail.
    """
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            result = _client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=text,
            )
            return result.embeddings[0].values
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Embedding attempt %d/%d failed: %s. Retrying in %.1fs...",
                    attempt + 1, MAX_RETRIES, e, delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "Embedding failed after %d attempts: %s", MAX_RETRIES, e
                )

    raise RuntimeError(
        f"Gemini embedding failed after {MAX_RETRIES} attempts: {last_error}"
    )
