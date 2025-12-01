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

# Track stop flags for LLM
llm_stop_flags = {}

app = FastAPI()

# Serve frontend
static_path = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_path), name="static")

AZURE_SPEECH_KEY = CONFIG["AZURE_SPEECH_KEY"]
AZURE_SPEECH_REGION = CONFIG["AZURE_SPEECH_REGION"]

print("🔐 Azure Speech Key Loaded:", AZURE_SPEECH_KEY[:5] + "****")
print("🌍 Region:", AZURE_SPEECH_REGION)

# -------------------------------------------------------
# AZURE TTS (Neural, returns PCM16 bytes)
# -------------------------------------------------------
speech_tts_config = speechsdk.SpeechConfig(
    subscription=AZURE_SPEECH_KEY,
    region=AZURE_SPEECH_REGION
)
speech_tts_config.speech_synthesis_voice_name = "en-US-JennyNeural"

tts_audio_format = speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm
speech_tts_config.set_speech_synthesis_output_format(tts_audio_format)

def azure_tts_generate(text: str) -> bytes:
    """Return PCM16 audio bytes using Azure Neural TTS."""
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_tts_config,
        audio_config=None
    )

    result = synthesizer.speak_text_async(text).get()

    if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
        return result.audio_data

    print("❌ TTS Error:", result.reason)
    return b""


# ======================================================
# TEXT CHAT HANDLER
# ======================================================
async def handle_text_message(ws: WebSocket, text: str, session: str):
    print("📝 Text message:", text)

    # Echo user bubble
    await ws.send_json({"type": "final", "text": text})

    llm_stop_flags[session] = False
    full_output = ""

    # ---- Stream LLM tokens ----
    async for token in stream_llm(text):
        if llm_stop_flags.get(session):
            print("⛔ Text LLM interrupted")
            break

        full_output += token
        await ws.send_json({"type": "llm_stream", "token": token})

    await ws.send_json({"type": "llm_done"})
    print("✨ Text LLM finished.")

    # ---- Word count cutoff rule ----
    word_count = len(full_output.split())
    print(f"📏 Word count: {word_count}")

    if word_count > 1000:
        spoken_msg = (
            "This is a long response. Please read the detailed text below "
            "or check the generated document."
        )
        audio = azure_tts_generate(spoken_msg)
        print("🔊 [TEXT MSG] Sending audio bytes:", len(audio))
        if audio:
            await ws.send_bytes(audio)
        return

    # Normal short response → full TTS
    if full_output.strip():
        audio = azure_tts_generate(full_output)
        print("🔊 [TEXT MSG] Sending audio bytes:", len(audio))
        if audio:
            await ws.send_bytes(audio)


# ======================================================
# MAIN STREAM (STT + LLM + CHAT)
# ======================================================
async def azure_stream(ws: WebSocket):
    session = str(uuid.uuid4())
    print(f"WS connected: {session}")

    # ---- Azure STT input stream ----
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

    # -------------------------------
    # PARTIAL STT callback
    # -------------------------------
    def recognizing(evt):
        asyncio.run_coroutine_threadsafe(
            ws.send_json({"type": "partial", "text": evt.result.text}),
            loop
        )

    # -------------------------------
    # FINAL STT callback
    # -------------------------------
    def recognized(evt):
        if evt.result.reason != speechsdk.ResultReason.RecognizedSpeech:
            return

        text = evt.result.text
        print("🟢 Final STT:", text)

        async def handle_final():
            await ws.send_json({"type": "final", "text": text})
            llm_stop_flags[session] = False
            full_output = ""

            # ---- LLM streaming ----
            try:
                async for token in stream_llm(text):
                    if llm_stop_flags.get(session):
                        print("⛔ STT LLM interrupted")
                        break
                    full_output += token
                    await ws.send_json({"type": "llm_stream", "token": token})
            except Exception as e:
                await ws.send_json({
                    "type": "llm_stream",
                    "token": f"[LLM ERROR] {e}"
                })
                print("❌ LLM stream error:", e)

            await ws.send_json({"type": "llm_done"})
            print("✨ STT LLM done.")

            # ---- Word count cutoff ----
            word_count = len(full_output.split())
            print(f"📏 Word count: {word_count}")

            if word_count > 1000:
                spoken_msg = (
                    "This is a long response. Please read the detailed text below "
                    "or check the generated document."
                )
                audio = azure_tts_generate(spoken_msg)
                print("🔊 [STT MSG] Sending audio bytes:", len(audio))
                if audio:
                    await ws.send_bytes(audio)
                return

            # ---- Normal TTS ----
            if full_output.strip():
                audio = azure_tts_generate(full_output)
                print("🔊 [STT MSG] Sending audio bytes:", len(audio))
                if audio:
                    await ws.send_bytes(audio)

        asyncio.run_coroutine_threadsafe(handle_final(), loop)

    recognizer.recognizing.connect(recognizing)
    recognizer.recognized.connect(recognized)

    recognizer.start_continuous_recognition_async().get()
    print("🎤 Azure STT started successfully")

    # ==================================================
    # MAIN WEBSOCKET LOOP
    # ==================================================
    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.disconnect":
                print("⚠ WS disconnect by client")
                break

            # Text JSON messages from frontend
            if msg.get("text"):
                try:
                    data = json.loads(msg["text"])
                except:
                    data = None

                if data:
                    if data.get("type") == "text":
                        asyncio.create_task(
                            handle_text_message(ws, data["text"], session)
                        )
                        continue

                    if data.get("type") == "stop_llm":
                        llm_stop_flags[session] = True
                        continue

            # Raw PCM audio
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
