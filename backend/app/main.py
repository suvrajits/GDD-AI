# backend/app/main.py
from dotenv import load_dotenv
load_dotenv()

import os
import uuid
import asyncio
import threading

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
import azure.cognitiveservices.speech as speechsdk
from .config import CONFIG

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

    push_stream = speechsdk.audio.PushAudioInputStream(
        stream_format=speechsdk.audio.AudioStreamFormat(samples_per_second=16000,
                                                        bits_per_sample=16,
                                                        channels=1)
    )

    recognizer = speechsdk.SpeechRecognizer(
        speech_config=speechsdk.SpeechConfig(
            subscription=AZURE_SPEECH_KEY,
            region=AZURE_SPEECH_REGION,
            speech_recognition_language="en-US"
        ),
        audio_config=speechsdk.audio.AudioConfig(stream=push_stream),
    )

    loop = asyncio.get_event_loop()

    def recognizing(evt):
        asyncio.run_coroutine_threadsafe(
            ws.send_json({"type": "partial", "text": evt.result.text}),
            loop
        )

    def recognized(evt):
        if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech:
            asyncio.run_coroutine_threadsafe(
                ws.send_json({"type": "final", "text": evt.result.text}),
                loop
            )

    recognizer.recognizing.connect(recognizing)
    recognizer.recognized.connect(recognized)

    threading.Thread(
        target=lambda: recognizer.start_continuous_recognition(),
        daemon=True
    ).start()

    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.disconnect":
                break

            if msg.get("bytes"):
                push_stream.write(msg["bytes"])

    finally:
        push_stream.close()
        recognizer.stop_continuous_recognition()
        print("WS closed:", session)


@app.websocket("/ws/stream")
async def websocket_stream(ws: WebSocket):
    await ws.accept()
    await azure_stream(ws)
