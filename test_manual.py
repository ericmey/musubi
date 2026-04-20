import asyncio
from httpx import AsyncClient, ASGITransport
from musubi.api.app import create_app
from musubi.settings import Settings
from httpx_sse import aconnect_sse

async def run():
    app = create_app()
    app.state.testing = True # To trigger fast pings
    # Needs valid token, so we just test the 401 first to see if it responds AT ALL
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/thoughts/stream", params={"namespace": "ns"})
        print(response.status_code)
        print(response.json())

if __name__ == "__main__":
    asyncio.run(run())
