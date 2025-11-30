# backend/app/main.py
from dotenv import load_dotenv
load_dotenv()

import os
import uuid
import asyncio

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
import azure.cognitiveservices.speech as speechsdk

from .config import CONFIG
from .llm_orchestrator import stream_llm


app = FastAPI()

static_path = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_path), name="static")


AZURE_SPEECH_KEY = CONFIG["AZURE_SPEECH_KEY"]
AZURE_SPEECH_REGION = CONFIG["AZURE_SPEECH_REGION"]

print("🔐 Azure Speech Key Loaded:", AZURE_SPEECH_KEY[:5] + "****")
print("🌍 Region:", AZURE_SPEECH_REGION)


async def azure_stream(ws: WebSocket):
    session = str(uuid.uuid4())
    print(f"WS connected: {session}")

    # -------------------------------
    # 1. Azure Speech Push Stream
    # -------------------------------
    push_stream = speechsdk.audio.PushAudioInputStream(
        stream_format=speechsdk.audio.AudioStreamFormat(
            samples_per_second=16000,
            bits_per_sample=16,
            channels=1
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

    # -------------------------------
    # PARTIAL STT CALLBACK
    # -------------------------------
    def recognizing(evt):
        asyncio.run_coroutine_threadsafe(
            ws.send_json({
                "type": "partial",
                "text": evt.result.text
            }),
            loop
        )

    # -------------------------------
    # FINAL STT CALLBACK
    # -------------------------------
    def recognized(evt):
        if evt.result.reason != speechsdk.ResultReason.RecognizedSpeech:
            return

        text = evt.result.text
        print("🟢 Final STT:", text)

        async def handle_final():
            # 1) Send STT transcript
            await ws.send_json({"type": "final", "text": text})

            print("🔥 LLM CALL ->", text)

            # 2) Stream LLM tokens
            try:
                async for token in stream_llm(text):
                    await ws.send_json({"type": "llm_stream", "token": token})
            except Exception as e:
                await ws.send_json({"type": "llm_stream", "token": f"[LLM ERROR] {e}"})
                print("❌ LLM Streaming Error:", e)

            # 3) Completion marker
            await ws.send_json({"type": "llm_done"})
            print("✨ LLM stream finished.")

        asyncio.run_coroutine_threadsafe(handle_final(), loop)

    recognizer.recognizing.connect(recognizing)
    recognizer.recognized.connect(recognized)

    # -------------------------------
    # START SPEECH RECOGNITION
    # -------------------------------
    recognizer.start_continuous_recognition_async().get()
    print("🎤 Azure STT started successfully")

    # -------------------------------
    # MAIN WS LOOP
    # -------------------------------
    try:
        while True:
            try:
                msg = await ws.receive()

                if msg["type"] == "websocket.disconnect":
                    print("⚠ Client requested WS close.")
                    break

                if msg.get("bytes"):
                    push_stream.write(msg["bytes"])

            except Exception as e:
                print("⚠ WS receive error:", e)
                break

    finally:
        print("🟡 Cleaning up STT + WS")

        try:
            push_stream.close()
        except:
            pass

        try:
            recognizer.stop_continuous_recognition()
        except Exception as e:
            print("⚠ Error stopping STT:", e)

        print("WS closed:", session)


@app.websocket("/ws/stream")
async def websocket_stream(ws: WebSocket):
    await ws.accept()
    await azure_stream(ws)
