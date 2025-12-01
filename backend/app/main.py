# backend/app/main.py
from dotenv import load_dotenv
load_dotenv()

import os
import uuid
import asyncio
import json
import re
from typing import Dict, List, Optional

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
import azure.cognitiveservices.speech as speechsdk

from .config import CONFIG
from .llm_orchestrator import stream_llm

# -----------------------
# Global state
# -----------------------
llm_stop_flags: Dict[str, bool] = {}
user_last_input_was_voice: Dict[str, bool] = {}
sentence_buffer: Dict[str, str] = {}

tts_queue: Dict[str, List[str]] = {}
tts_worker_running: Dict[str, bool] = {}
tts_worker_tasks: Dict[str, asyncio.Task] = {}
tts_cancel_events: Dict[str, asyncio.Event] = {}

# -----------------------
# Constants
# -----------------------
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
FAST_PADDING = 0.01  # 10ms
MIN_SPOKEN_CHARS = 3

# -----------------------
# FastAPI app
# -----------------------
app = FastAPI()
static_path = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_path), name="static")

AZURE_SPEECH_KEY = CONFIG["AZURE_SPEECH_KEY"]
AZURE_SPEECH_REGION = CONFIG["AZURE_SPEECH_REGION"]

print("🔐 Azure Speech Key Loaded:", AZURE_SPEECH_KEY[:5] + "****")
print("🌍 Region:", AZURE_SPEECH_REGION)

# -----------------------
# Markdown cleaner
# -----------------------
def clean_sentence_for_tts(text: str) -> str:
    if not text:
        return ""
    text = text.replace("#", " ")
    text = re.sub(r"[*_`~]+", "", text)
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

# -----------------------
# Azure TTS config
# -----------------------
speech_tts_config = speechsdk.SpeechConfig(
    subscription=AZURE_SPEECH_KEY,
    region=AZURE_SPEECH_REGION,
)
speech_tts_config.speech_synthesis_voice_name = "en-US-JennyNeural"
speech_tts_config.set_speech_synthesis_output_format(
    speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm
)

def azure_tts_generate_sync(text: str) -> bytes:
    """Blocking call — run in thread."""
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_tts_config,
        audio_config=None
    )
    result = synthesizer.speak_text_async(text).get()
    if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
        return result.audio_data
    print("❌ Azure TTS Error:", result.reason)
    return b""

# -----------------------
# TTS worker
# -----------------------
async def tts_worker(ws: WebSocket, session: str):
    """
    Sequentially consume tts_queue[session]. Honor tts_cancel_events[session]
    to prevent sending audio after cancel. When finished, send "voice_done".
    """
    if tts_worker_running.get(session):
        return

    tts_worker_running[session] = True
    tts_cancel_events[session] = asyncio.Event()
    print(f"▶️ TTS worker started for {session}")

    try:
        while True:
            queue = tts_queue.get(session, [])
            if not queue:
                break

            if llm_stop_flags.get(session):
                print("🔇 stop flag set — clearing queue")
                queue.clear()
                break

            # pop next sentence
            sentence = queue.pop(0).strip()
            if len(sentence) < MIN_SPOKEN_CHARS:
                continue

            clean = clean_sentence_for_tts(sentence)
            if not clean:
                continue

            # If cancel was requested before we reveal, stop
            if tts_cancel_events[session].is_set():
                print("🔇 canceled before reveal")
                break

            # Reveal sentence in UI
            try:
                await ws.send_json({"type": "sentence_start", "text": clean})
            except Exception:
                print("❌ ws.send_json failed (client probably disconnected). stop worker.")
                break

            # Generate audio in thread; can't forcibly cancel the thread but we will drop output if canceled
            try:
                audio_bytes = await asyncio.to_thread(azure_tts_generate_sync, clean)
            except asyncio.CancelledError:
                print("🔇 tts generation task cancelled")
                break
            except Exception as e:
                print("❌ exception during azure tts generate:", e)
                audio_bytes = b""

            # If cancellation occurred while generating, skip sending
            if tts_cancel_events[session].is_set() or llm_stop_flags.get(session):
                print("🔇 canceled during generation — dropping audio")
                continue

            if audio_bytes:
                try:
                    await ws.send_bytes(audio_bytes)
                except Exception as e:
                    print("❌ failed to send audio bytes:", e)
                    # client closed mid-playback — stop worker
                    break

                byte_len = len(audio_bytes)
                duration = byte_len / (SAMPLE_RATE * BYTES_PER_SAMPLE)
                # Wait for duration + tiny padding, but if cancel requested, break early
                try:
                    await asyncio.sleep(duration + FAST_PADDING)
                except asyncio.CancelledError:
                    print("🔇 tts worker sleep cancelled")
                    break
            else:
                await asyncio.sleep(FAST_PADDING)

        # finished queue; signal UI to clear voice-mode
        try:
            await ws.send_json({"type": "voice_done"})
        except:
            pass

    finally:
        tts_worker_running[session] = False
        tts_cancel_events.pop(session, None)
        tts_worker_tasks.pop(session, None)
        # clear queue to free memory
        if session in tts_queue and not tts_queue[session]:
            tts_queue.pop(session, None)
        print(f"⏹ TTS worker finished for {session}")

# helper to start worker
def ensure_tts_worker(ws: WebSocket, session: str):
    if not tts_worker_running.get(session):
        task = asyncio.create_task(tts_worker(ws, session))
        tts_worker_tasks[session] = task

def enqueue_sentence(session: str, ws: WebSocket, sentence: str):
    if not sentence or not sentence.strip():
        return
    if session not in tts_queue:
        tts_queue[session] = []
    tts_queue[session].append(sentence)
    ensure_tts_worker(ws, session)

# -----------------------
# Typed text handler (no TTS)
# -----------------------
async def handle_text_message(ws: WebSocket, text: str, session: str):
    text = (text or "").strip()
    if not text:
        print("⚠ ignoring empty typed message")
        return

    print("📝 Text message:", text)
    user_last_input_was_voice[session] = False
    sentence_buffer[session] = ""
    await ws.send_json({"type": "final", "text": text})

    llm_stop_flags[session] = False

    async for token in stream_llm(text):
        if llm_stop_flags.get(session):
            print("⛔ text LLM interrupted")
            break
        await ws.send_json({"type": "llm_stream", "token": token})

    await ws.send_json({"type": "llm_done"})
    print("✨ Text LLM done.")

# -----------------------
# Main STT + LLM + TTS
# -----------------------
async def azure_stream(ws: WebSocket):
    session = str(uuid.uuid4())
    llm_stop_flags[session] = False
    user_last_input_was_voice[session] = False
    sentence_buffer[session] = ""
    print("WS connected:", session)

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
            speech_recognition_language="en-US",
        ),
        audio_config=speechsdk.audio.AudioConfig(stream=push_stream),
    )

    loop = asyncio.get_event_loop()

    # partial recognizing callback -> used for barge-in detection
    def recognizing(evt):
        text = (evt.result.text or "").strip()
        # if user started producing meaningful audio while TTS is playing -> treat as barge-in
        if text and text not in [".", "uh", "um"]:
            # cancel any TTS worker/send stop message to client
            llm_stop_flags[session] = True
            # set cancel event so worker drops audio
            if session in tts_cancel_events:
                tts_cancel_events[session].set()
            # clear TTS queue
            if session in tts_queue:
                tts_queue[session].clear()
            # also cancel the worker task if present (will attempt graceful stop)
            task = tts_worker_tasks.get(session)
            if task and not task.done():
                try:
                    task.cancel()
                except:
                    pass
            # notify client to stop playback immediately
            asyncio.run_coroutine_threadsafe(ws.send_json({"type": "stop_all"}), loop)

        # send partial transcripts optionally
        if text and text not in ["", ".", "uh", "um"]:
            asyncio.run_coroutine_threadsafe(ws.send_json({"type": "partial", "text": text}), loop)

    # final recognized callback
    def recognized(evt):
        if evt.result.reason != speechsdk.ResultReason.RecognizedSpeech:
            return

        text = (evt.result.text or "").strip()
        if text in ["", ".", "uh", "um"]:
            print("⚠ Ignoring garbage STT final:", text)
            return

        print("🟢 Final STT:", text)
        user_last_input_was_voice[session] = True
        sentence_buffer[session] = ""

        async def handle_final():
            await ws.send_json({"type": "final", "text": text})
            llm_stop_flags[session] = False
            full_output = ""
            sentence_buffer[session] = ""

            try:
                async for token in stream_llm(text):
                    if llm_stop_flags.get(session):
                        print("⛔ STT LLM interrupted")
                        break

                    full_output += token
                    sentence_buffer[session] += token

                    buff = sentence_buffer[session].strip()
                    if not buff:
                        continue

                    if buff.endswith((".", "!", "?", "...")) or len(buff.split()) >= 60:
                        enqueue_sentence(session, ws, buff)
                        sentence_buffer[session] = ""

            except Exception as e:
                await ws.send_json({"type": "llm_stream", "token": f"[LLM ERROR] {e}"})
                print("❌ LLM stream error:", e)

            # flush leftover
            leftover = sentence_buffer.get(session, "").strip()
            if leftover:
                enqueue_sentence(session, ws, leftover)
                sentence_buffer[session] = ""

            await ws.send_json({"type": "llm_done"})
            print("✨ STT LLM done.")

        asyncio.run_coroutine_threadsafe(handle_final(), loop)

    recognizer.recognizing.connect(recognizing)
    recognizer.recognized.connect(recognized)
    recognizer.start_continuous_recognition_async().get()
    print("🎤 Azure STT started successfully")

    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.disconnect":
                print("⚠ WS disconnect")
                break

            # JSON control messages
            if msg.get("text"):
                try:
                    data = json.loads(msg["text"])
                except:
                    data = None

                if data:
                    if data.get("type") == "text":
                        # typed input bypasses voice flow
                        asyncio.create_task(handle_text_message(ws, data["text"], session))
                        continue

                    if data.get("type") == "stop_llm":
                        # Manual stop - immediately cancel generation and playback
                        llm_stop_flags[session] = True
                        if session in tts_cancel_events:
                            tts_cancel_events[session].set()
                        if session in tts_queue:
                            tts_queue[session].clear()
                        # cancel worker task
                        task = tts_worker_tasks.get(session)
                        if task and not task.done():
                            try:
                                task.cancel()
                            except:
                                pass
                        # notify client to stop audio immediately
                        await ws.send_json({"type": "stop_all"})
                        continue

            # Raw PCM bytes -> push to Azure STT
            if msg.get("bytes"):
                try:
                    push_stream.write(msg["bytes"])
                except Exception:
                    pass

    finally:
        print("🟡 Cleaning session", session)
        try: push_stream.close()
        except: pass
        try: recognizer.stop_continuous_recognition()
        except: pass

        # cancel worker tasks if present
        task = tts_worker_tasks.get(session)
        if task and not task.done():
            try:
                task.cancel()
            except:
                pass

        llm_stop_flags.pop(session, None)
        user_last_input_was_voice.pop(session, None)
        sentence_buffer.pop(session, None)
        tts_queue.pop(session, None)
        tts_worker_running.pop(session, None)
        tts_worker_tasks.pop(session, None)
        tts_cancel_events.pop(session, None)

        print("WS closed:", session)

@app.websocket("/ws/stream")
async def websocket_stream(ws: WebSocket):
    await ws.accept()
    await azure_stream(ws)
