# app/main.py
from dotenv import load_dotenv
load_dotenv()

import os
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

# Keep router imports (they were in original main.py). These modules must exist.
from .config import CONFIG
from app.routes.rag_routes import router as rag_router
from app.gdd_api import router as gdd_router

# Import the stream handler (moved to stream_engine)
from .stream_engine import azure_stream

app = FastAPI()
static_path = os.path.join(os.path.dirname(__file__), "static")
app.include_router(rag_router)
app.mount("/static", StaticFiles(directory=static_path), name="static")
app.include_router(gdd_router, prefix="/gdd", tags=["GDD"])

# Print some basic config info (kept from original)
AZURE_SPEECH_KEY = CONFIG["AZURE_SPEECH_KEY"]
AZURE_SPEECH_REGION = CONFIG["AZURE_SPEECH_REGION"]

print("🔐 Azure Speech Key Loaded:", AZURE_SPEECH_KEY[:5] + "****")
print("🌍 Region:", AZURE_SPEECH_REGION)

@app.websocket("/ws/stream")
async def websocket_stream(ws: WebSocket):
    """
    Tiny bootstrap: accept and hand-off to the streaming engine.
    """
    await ws.accept()
    await azure_stream(ws)
