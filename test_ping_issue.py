import asyncio
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import uvicorn

app = FastAPI()

async def gen():
    while True:
        try:
            await asyncio.sleep(0.1)
            yield b"data: ping\n\n"
        except asyncio.CancelledError:
            break

@app.get("/stream")
async def stream():
    return StreamingResponse(gen(), media_type="text/event-stream")
