import asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from httpx_sse import aconnect_sse
import pytest
from musubi.api.app import create_app
from musubi.settings import Settings

async def test_endpoint():
    app = create_app()
    # Need to setup fake auth/settings here or we get 401...
    # The actual tests are hanging because `aconnect_sse` never finishes
    # getting its first event if the generator yields nothing?
    print("Test running")

if __name__ == "__main__":
    asyncio.run(test_endpoint())
