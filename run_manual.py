import asyncio
from httpx import AsyncClient, ASGITransport
import pytest
import sys
import os

sys.path.insert(0, os.path.abspath("src"))

from musubi.api.app import create_app
from musubi.settings import Settings

async def run():
    app = create_app(settings=Settings(
        qdrant_host="qdrant",
        qdrant_api_key="test-qdrant-key",
        tei_dense_url="http://tei-dense",
        tei_sparse_url="http://tei-sparse",
        tei_reranker_url="http://tei-reranker",
        ollama_url="http://ollama:11434",
        embedding_model="BAAI/bge-m3",
        sparse_model="naver/splade-v3",
        reranker_model="BAAI/bge-reranker-v2-m3",
        llm_model="qwen2.5:7b-instruct-q4_K_M",
        vault_path="/tmp/vault",
        artifact_blob_path="/tmp/artifacts",
        lifecycle_sqlite_path="/tmp/lifecycle.sqlite",
        log_dir="/tmp/logs",
        jwt_signing_key="a-very-long-test-signing-key-for-hs256-tokens-32+bytes",
        oauth_authority="https://auth.example.test",
    ))
    # Needs valid token, so we just test the 401 first to see if it responds AT ALL
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/thoughts/stream", params={"namespace": "ns"})
        print(response.status_code)
        print(response.json())

if __name__ == "__main__":
    asyncio.run(run())
