# backend/app/main.py
from dotenv import load_dotenv
load_dotenv()

import os
import uuid
import asyncio
import json

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
import azure.cognitiveservices.speech as speechsdk

from .config import CONFIG
from .llm_orchestrator import stream_llm

# Track manual stop flags for LLM
llm_stop_flags = {}

app = FastAPI()

# Serve static files
static_path = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_path), name="static")

AZURE_SPEECH_KEY = CONFIG["AZURE_SPEECH_KEY"]
AZURE_SPEECH_REGION = CONFIG["AZURE_SPEECH_REGION"]

print("🔐 Azure Speech Key Loaded:", AZURE_SPEECH_KEY[:5] + "****")
print("🌍 Region:", AZURE_SPEECH_REGION)


# --------------------------------------------------
# TEXT CHAT HANDLER (manual messages)
# --------------------------------------------------
async def handle_text_message(ws: WebSocket, text: str, session: str):
    print("📝 Text message:", text)

    # Deliver to UI as "user" bubble
    await ws.send_json({"type": "final", "text": text})

    # Reset stop flag for this session
    llm_stop_flags[session] = False

    # Stream LLM tokens
    async for token in stream_llm(text):
        if llm_stop_flags.get(session):
            print("⛔ Text LLM interrupted")
            break
        await ws.send_json({"type": "llm_stream", "token": token})

    await ws.send_json({"type": "llm_done"})
    print("✨ Text LLM response finished.")


# --------------------------------------------------
# MAIN STREAM (STT + Text Chat + LLM)
# --------------------------------------------------
async def azure_stream(ws: WebSocket):
    session = str(uuid.uuid4())
    print(f"WS connected: {session}")

    # Setup Azure audio stream
    push_stream = speechsdk.audio.PushAudioInputStream(
        stream_format=speechsdk.audio.AudioStreamFormat(
            samples_per_second=16000,
            bits_per_sample=16,
            channels=1,
        )
    )

    recognizer = speechsdk.SpeechRecognizer(
        speech_config=speechsdk.SpeechConfig(
            subscription=AZURE_SPEECH_KEY,
            region=AZURE_SPEECH_REGION,
            speech_recognition_language="en-US",
        ),
        audio_config=speechsdk.audio.AudioConfig(stream=push_stream),
    )

    loop = asyncio.get_event_loop()

    # Partial STT
    def recognizing(evt):
        asyncio.run_coroutine_threadsafe(
            ws.send_json({
                "type": "partial",
                "text": evt.result.text
            }),
            loop
        )

    # Final STT
    def recognized(evt):
        if evt.result.reason != speechsdk.ResultReason.RecognizedSpeech:
            return

        text = evt.result.text
        print("🟢 Final STT:", text)

        async def handle_final_stt():
            await ws.send_json({"type": "final", "text": text})

            llm_stop_flags[session] = False

            try:
                async for token in stream_llm(text):
                    if llm_stop_flags.get(session):
                        print("⛔ STT LLM interrupted")
                        break
                    await ws.send_json({"type": "llm_stream", "token": token})
            except Exception as e:
                await ws.send_json({
                    "type": "llm_stream",
                    "token": f"[LLM ERROR] {e}"
                })
                print("❌ LLM Stream error:", e)

            await ws.send_json({"type": "llm_done"})
            print("✨ STT LLM done.")

        asyncio.run_coroutine_threadsafe(handle_final_stt(), loop)

    recognizer.recognizing.connect(recognizing)
    recognizer.recognized.connect(recognized)

    recognizer.start_continuous_recognition_async().get()
    print("🎤 Azure STT started successfully")

    # --------------------------------------------------
    # MAIN WEBSOCKET LOOP
    # --------------------------------------------------
    try:
        while True:
            msg = await ws.receive()

            # Client closed WS
            if msg["type"] == "websocket.disconnect":
                print("⚠ WS disconnect by client")
                break

            # ----------------------------------------------
            # TEXT message (JSON from chat box)
            # ----------------------------------------------
            if msg.get("text"):
                try:
                    data = json.loads(msg["text"])
                except:
                    data = None

                if data:
                    # Manual chat text
                    if data.get("type") == "text":
                        asyncio.create_task(
                            handle_text_message(ws, data["text"], session)
                        )
                        continue

                    # Explicit stop
                    if data.get("type") == "stop_llm":
                        llm_stop_flags[session] = True
                        continue

            # ----------------------------------------------
            # AUDIO BYTES (PCM)
            # ----------------------------------------------
            if msg.get("bytes"):
                push_stream.write(msg["bytes"])

    finally:
        print(f"🟡 Cleaning WS session {session}")

        try:
            push_stream.close()
        except:
            pass

        try:
            recognizer.stop_continuous_recognition()
        except:
            pass

        print(f"WS closed: {session}")


@app.websocket("/ws/stream")
async def websocket_stream(ws: WebSocket):
    await ws.accept()
    await azure_stream(ws)
