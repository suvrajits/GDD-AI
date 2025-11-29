# backend/app/main.py
import json
import uuid
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .config import CONFIG
from .speech_engine import AzureSpeechStream
from .stablebuffer import StableBuffer
from .llm_orchestrator import call_llm

from fastapi.staticfiles import StaticFiles
import os


app = FastAPI(title="Realtime Agentic POC")

# Serve static frontend files
static_path = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_path), name="static")


AZURE_SPEECH_KEY = CONFIG["AZURE_SPEECH_KEY"]
AZURE_SPEECH_REGION = CONFIG["AZURE_SPEECH_REGION"]


@app.get("/")
def root():
    return {"message": "Backend running"}


@app.websocket("/ws/stream")
async def websocket_stream(ws: WebSocket):
    await ws.accept()
    conversation_id = str(uuid.uuid4())
    print(f"🔌 WebSocket connected: {conversation_id}")

    # Stable buffer
    buffer = StableBuffer()

    # Azure Speech engine (with correct args)
    stt = AzureSpeechStream(AZURE_SPEECH_KEY, AZURE_SPEECH_REGION)

    # -----------------------------
    # Callback: Partial
    # -----------------------------
    async def on_partial(text: str):
        try:
            merged = buffer.update_partial(text)
            await ws.send_json({"type": "partial", "text": merged})
        except Exception as e:
            print("⚠️ on_partial error:", e)

    # -----------------------------
    # Callback: Final
    # -----------------------------
    async def on_final(text: str):
        try:
            committed = buffer.commit_final(text)
            await ws.send_json({"type": "final", "text": committed})

            # LLM
            llm_reply = await call_llm(committed)
            await ws.send_json({
                "type": "llm",
                "conversation_id": conversation_id,
                "content": llm_reply
            })

        except Exception as e:
            print("⚠️ on_final error:", e)

    # Wire callbacks
    stt.set_callbacks(on_partial, on_final)

    # Start STT (non-await)
    stt.start()
    print("🎤 Azure Speech recognition started")

    try:
        while True:
            msg = await ws.receive()

            # Client disconnected
            if msg["type"] == "websocket.disconnect":
                print("🔌 Client disconnected")
                break

            # Binary audio frame
            if msg["type"] == "websocket.receive" and "bytes" in msg:
                try:
                    await stt.push_audio(msg["bytes"])
                except Exception as e:
                    print("⚠️ Error pushing audio:", e)

            # Optional text commands
            elif msg["type"] == "websocket.receive" and "text" in msg:
                try:
                    cmd = json.loads(msg["text"]).get("cmd")
                except:
                    cmd = None

                if cmd == "stop":
                    break

                await ws.send_json({"type": "info", "msg": "unknown command"})

    except WebSocketDisconnect:
        print("⚠️ WebSocket disconnect")
    except Exception as e:
        print("❌ Unexpected websocket error:", e)

    finally:
        try:
            stt.stop()      # IMPORTANT: no await
        except Exception as e:
            print("⚠️ Error stopping STT:", e)

        try:
            await ws.close()
        except:
            pass

        print(f"🛑 Clean shutdown of session {conversation_id}")
