import asyncio
from httpx import AsyncClient, ASGITransport
from test_ping_issue import app

async def run():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream("GET", "/stream") as resp:
            async for line in resp.aiter_lines():
                if line.strip():
                    print("Received:", line)
                    break
    print("Exited stream context block successfully")

if __name__ == "__main__":
    asyncio.run(run())
