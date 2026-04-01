"""Tests for embedding with retry logic."""

from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import VECTOR_SIZE


class TestEmbedText:
    def test_success_first_try(self):
        fake_embedding = MagicMock()
        fake_embedding.values = [0.1] * VECTOR_SIZE

        fake_result = MagicMock()
        fake_result.embeddings = [fake_embedding]

        with patch("musubi.embedding._client") as mock_client:
            mock_client.models.embed_content.return_value = fake_result

            from musubi.embedding import embed_text

            result = embed_text("test text")
            assert len(result) == VECTOR_SIZE
            mock_client.models.embed_content.assert_called_once()

    def test_retry_on_failure_then_success(self):
        fake_embedding = MagicMock()
        fake_embedding.values = [0.1] * VECTOR_SIZE

        fake_result = MagicMock()
        fake_result.embeddings = [fake_embedding]

        with patch("musubi.embedding._client") as mock_client:
            mock_client.models.embed_content.side_effect = [
                Exception("API error"),
                fake_result,
            ]

            with patch("musubi.embedding.time.sleep"):
                from musubi.embedding import embed_text

                result = embed_text("test text")
                assert len(result) == VECTOR_SIZE
                assert mock_client.models.embed_content.call_count == 2

    def test_all_retries_exhausted(self):
        with patch("musubi.embedding._client") as mock_client:
            mock_client.models.embed_content.side_effect = Exception("API down")

            with patch("musubi.embedding.time.sleep"):
                from musubi.embedding import embed_text

                with pytest.raises(RuntimeError, match="failed after 3 attempts"):
                    embed_text("test text")
