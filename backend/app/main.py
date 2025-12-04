# backend/app/main.py
from dotenv import load_dotenv
load_dotenv()

import os
import uuid
import asyncio
import json
import re
from typing import Dict, List

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
import azure.cognitiveservices.speech as speechsdk

from .config import CONFIG
from .llm_orchestrator import stream_llm
from app.routes.rag_routes import router as rag_router
from .gdd_api import router as gdd_router

# -----------------------
# GLOBAL STATE
# -----------------------
llm_stop_flags: Dict[str, bool] = {}
user_last_input_was_voice: Dict[str, bool] = {}
sentence_buffer: Dict[str, str] = {}

tts_sentence_queue: Dict[str, List[str]] = {}
tts_gen_tasks: Dict[str, List[asyncio.Task]] = {}
tts_cancel_events: Dict[str, asyncio.Event] = {}
tts_playback_task: Dict[str, asyncio.Task] = {}

playback_ws_registry: Dict[str, WebSocket] = {}
assistant_is_speaking: Dict[str, bool] = {}

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2  # 16-bit PCM

MIN_PADDING = 0.02      # 20ms
MAX_PADDING = 0.08      # 80ms

# -----------------------
# FASTAPI APP
# -----------------------
app = FastAPI()
static_path = os.path.join(os.path.dirname(__file__), "static")
app.include_router(rag_router)
app.mount("/static", StaticFiles(directory=static_path), name="static")
# GDD API routes
app.include_router(gdd_router, prefix="/gdd", tags=["GDD"])


AZURE_SPEECH_KEY = CONFIG["AZURE_SPEECH_KEY"]
AZURE_SPEECH_REGION = CONFIG["AZURE_SPEECH_REGION"]

print("🔐 Azure Speech Key Loaded:", AZURE_SPEECH_KEY[:5] + "****")
print("🌍 Region:", AZURE_SPEECH_REGION)

# -----------------------
# UTILITIES
# -----------------------
def clean_sentence_for_tts(text: str) -> str:
    if not text:
        return ""
    text = text.replace("#", " ")
    text = re.sub(r"[*_`~]+", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def adaptive_padding(sentence: str) -> float:
    if not sentence:
        return MIN_PADDING
    words = len(sentence.split())
    pad = MIN_PADDING + (words / 50.0) * 0.02
    return max(MIN_PADDING, min(MAX_PADDING, pad))

# -----------------------
# AZURE TTS (SYNC, BLOCKING)
# -----------------------
speech_tts_config = speechsdk.SpeechConfig(
    subscription=AZURE_SPEECH_KEY,
    region=AZURE_SPEECH_REGION
)
speech_tts_config.speech_synthesis_voice_name = "en-US-JennyNeural"
speech_tts_config.set_speech_synthesis_output_format(
    speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm
)

def azure_tts_generate_sync(text: str) -> bytes:
    synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_tts_config, audio_config=None)
    result = synthesizer.speak_text_async(text).get()

    if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
        return result.audio_data

    print("❌ Azure TTS error:", result.reason)
    return b""

async def async_tts(text: str) -> bytes:
    return await asyncio.to_thread(azure_tts_generate_sync, text)

# -----------------------
# STRUCT HELPERS
# -----------------------
def ensure_structs(session: str):
    tts_sentence_queue.setdefault(session, [])
    tts_gen_tasks.setdefault(session, [])
    tts_cancel_events.setdefault(session, asyncio.Event())
    assistant_is_speaking.setdefault(session, False)

def cancel_tts_generation(session: str):
    """Cancel ongoing TTS generation tasks + clear queue."""
    ev = tts_cancel_events.get(session)
    if ev:
        ev.set()
    for t in tts_gen_tasks.get(session, []):
        if not t.done():
            try: t.cancel()
            except: pass
    tts_sentence_queue[session] = []
    tts_gen_tasks[session] = []

# -----------------------
# PLAYBACK WORKER (NEVER OVERLAPS)
# -----------------------
async def tts_playback_worker(session: str):
    ws = playback_ws_registry.get(session)
    if not ws:
        return

    print(f"▶️ Playback worker started for {session}")
    ensure_structs(session)

    try:
        while True:
            if tts_cancel_events[session].is_set():
                print("🔇 Worker sees cancel -> exiting")
                break

            if not tts_gen_tasks[session]:
                if not tts_sentence_queue[session]:
                    break
                await asyncio.sleep(0.01)
                continue

            gen_task = tts_gen_tasks[session].pop(0)
            sentence_text = ""
            if tts_sentence_queue[session]:
                sentence_text = tts_sentence_queue[session].pop(0)

            if tts_cancel_events[session].is_set():
                try: gen_task.cancel()
                except: pass
                break

            # Reveal to UI only for voice mode
            if sentence_text and user_last_input_was_voice.get(session, False):
                try:
                    await ws.send_json({
                        "type": "sentence_start",
                        "text": sentence_text
                    })
                except:
                    break

            # WAIT FOR AUDIO
            try:
                audio_bytes = await gen_task
            except:
                audio_bytes = b""

            if not audio_bytes:
                continue

            assistant_is_speaking[session] = True

            try:
                await ws.send_bytes(audio_bytes)
            except Exception:
                assistant_is_speaking[session] = False
                break

            duration = len(audio_bytes) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
            pad = adaptive_padding(sentence_text)

            try:
                await asyncio.sleep(duration + pad)
            except asyncio.CancelledError:
                assistant_is_speaking[session] = False
                break

            assistant_is_speaking[session] = False

        try:
            await ws.send_json({"type": "voice_done"})
        except:
            pass

    finally:
        # ***IMPORTANT FIX*** — keep session alive, only reset
        print(f"⏹ Playback worker finished for {session} — RESETTING STATE")

        # Reset only queue/state, DO NOT delete session or registry
        tts_sentence_queue[session] = []
        tts_gen_tasks[session] = []
        tts_cancel_events[session] = asyncio.Event()
        assistant_is_speaking[session] = False

# -----------------------
# TEXT MESSAGE HANDLER
# -----------------------
async def handle_text_message(ws: WebSocket, text: str, session: str):
    text = text.strip()
    if not text:
        return

    user_last_input_was_voice[session] = False
    await ws.send_json({"type": "final", "text": text})

    llm_stop_flags[session] = False

    async for token in stream_llm(text):
        if llm_stop_flags[session]:
            break
        await ws.send_json({"type": "llm_stream", "token": token})

    await ws.send_json({"type": "llm_done"})

# -----------------------
# MAIN STREAM (STT + LLM + TTS)
# -----------------------
async def azure_stream(ws: WebSocket):
    session = str(uuid.uuid4())
    print("WS connected:", session)

    llm_stop_flags[session] = False
    user_last_input_was_voice[session] = False
    sentence_buffer[session] = ""

    ensure_structs(session)
    playback_ws_registry[session] = ws

    # Azure STT audio input stream
    push_stream = speechsdk.audio.PushAudioInputStream(
        stream_format=speechsdk.audio.AudioStreamFormat(
            samples_per_second=SAMPLE_RATE,
            bits_per_sample=16,
            channels=1,
        )
    )

    recognizer = speechsdk.SpeechRecognizer(
        speech_config=speechsdk.SpeechConfig(
            subscription=AZURE_SPEECH_KEY,
            region=AZURE_SPEECH_REGION,
            speech_recognition_language="en-US"
        ),
        audio_config=speechsdk.audio.AudioConfig(stream=push_stream)
    )

    loop = asyncio.get_event_loop()

    # PARTIAL STT (barge-in only if assistant is speaking)
    def recognizing(evt):
        text = (evt.result.text or "").strip()
        if text:
            asyncio.run_coroutine_threadsafe(
                ws.send_json({"type": "partial", "text": text}), loop
            )

        if assistant_is_speaking.get(session, False) and text not in ["", ".", "uh", "um"]:
            print("🔇 TRUE BARGE-IN DETECTED — cancelling playback + TTS generation")

            llm_stop_flags[session] = True

            if session in tts_cancel_events:
                tts_cancel_events[session].set()

            cancel_tts_generation(session)

            # cancel playback worker
            worker = tts_playback_task.get(session)
            if worker and not worker.done():
                try: worker.cancel()
                except: pass

            assistant_is_speaking[session] = False

            asyncio.run_coroutine_threadsafe(
                ws.send_json({"type": "stop_all"}), loop
            )

    # FINAL STT
    def recognized(evt):
        if evt.result.reason != speechsdk.ResultReason.RecognizedSpeech:
            return

        text = evt.result.text.strip()
        if not text or text in [".", "uh", "um"]:
            print("⚠ Ignoring garbage STT final:", text)
            return

        print("🟢 Final STT:", text)
        user_last_input_was_voice[session] = True
        sentence_buffer[session] = ""

        async def handle_final():
            await ws.send_json({"type": "final", "text": text})
            llm_stop_flags[session] = False
            sentence_buffer[session] = ""

            try:
                async for token in stream_llm(text):
                    if llm_stop_flags[session]:
                        break

                    sentence_buffer[session] += token
                    buf = sentence_buffer[session].strip()
                    if not buf:
                        continue

                    if buf.endswith((".", "!", "?", "...")) or len(buf.split()) >= 50:
                        cleaned = clean_sentence_for_tts(buf)
                        if cleaned:
                            tts_sentence_queue[session].append(cleaned)
                            task = asyncio.create_task(async_tts(cleaned))
                            tts_gen_tasks[session].append(task)

                        sentence_buffer[session] = ""

                        # ensure worker
                        if session not in tts_playback_task or tts_playback_task[session].done():
                            tts_playback_task[session] = asyncio.create_task(
                                tts_playback_worker(session)
                            )

            except Exception as e:
                await ws.send_json({"type": "llm_stream", "token": f"[ERR] {e}"})

            leftover = sentence_buffer[session].strip()
            if leftover:
                cleaned = clean_sentence_for_tts(leftover)
                if cleaned:
                    tts_sentence_queue[session].append(cleaned)
                    task = asyncio.create_task(async_tts(cleaned))
                    tts_gen_tasks[session].append(task)

                sentence_buffer[session] = ""

                if session not in tts_playback_task or tts_playback_task[session].done():
                    tts_playback_task[session] = asyncio.create_task(
                        tts_playback_worker(session)
                    )

            await ws.send_json({"type": "llm_done"})

        asyncio.run_coroutine_threadsafe(handle_final(), loop)

    recognizer.recognizing.connect(recognizing)
    recognizer.recognized.connect(recognized)
    recognizer.start_continuous_recognition_async().get()
    print("🎤 Azure STT started successfully")

    # MAIN WS LOOP
    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.disconnect":
                break

            # JSON control messages
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
                        print("⛔ Manual STOP for session", session)

                        llm_stop_flags[session] = True

                        if session in tts_cancel_events:
                            tts_cancel_events[session].set()

                        cancel_tts_generation(session)

                        worker = tts_playback_task.get(session)
                        if worker and not worker.done():
                            try: worker.cancel()
                            except: pass

                        # reset
                        tts_cancel_events[session] = asyncio.Event()
                        assistant_is_speaking[session] = False

                        await ws.send_json({"type": "stop_all"})
                        continue

            # AUDIO BYTES → STT
            if msg.get("bytes"):
                try:
                    push_stream.write(msg["bytes"])
                except:
                    pass

    finally:
        print("🟡 Cleaning session", session)
        try: push_stream.close()
        except: pass
        try: recognizer.stop_continuous_recognition()
        except: pass

        # Clean up
        cancel_tts_generation(session)

        worker = tts_playback_task.get(session)
        if worker and not worker.done():
            try: worker.cancel()
            except: pass

        llm_stop_flags.pop(session, None)
        user_last_input_was_voice.pop(session, None)
        sentence_buffer.pop(session, None)

        tts_sentence_queue.pop(session, None)
        tts_gen_tasks.pop(session, None)
        tts_cancel_events.pop(session, None)
        tts_playback_task.pop(session, None)

        playback_ws_registry.pop(session, None)
        assistant_is_speaking.pop(session, None)

        print("WS closed:", session)


@app.websocket("/ws/stream")
async def websocket_stream(ws: WebSocket):
    await ws.accept()
    await azure_stream(ws)
