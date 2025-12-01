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

# -----------------------
# Global state
# -----------------------
llm_stop_flags: Dict[str, bool] = {}
user_last_input_was_voice: Dict[str, bool] = {}
sentence_buffer: Dict[str, str] = {}

# TTS pipeline structures
tts_sentence_queue: Dict[str, List[str]] = {}
tts_gen_tasks: Dict[str, List[asyncio.Task]] = {}
tts_playback_task: Dict[str, asyncio.Task] = {}
tts_cancel_events: Dict[str, asyncio.Event] = {}
tts_worker_running: Dict[str, bool] = {}

# registry for the websocket so playback worker can send
playback_ws_registry: Dict[str, WebSocket] = {}

# assistant speaking state used for barge-in detection
assistant_is_speaking: Dict[str, bool] = {}

# -----------------------
# Constants
# -----------------------
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2  # 16-bit PCM
MIN_PADDING = 0.02    # 20ms
MAX_PADDING = 0.08    # 80ms

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
# Utilities
# -----------------------
def clean_sentence_for_tts(text: str) -> str:
    """Remove common markdown/formatting that confuses TTS and collapse whitespace."""
    if not text:
        return ""
    text = text.replace("#", " ")
    text = re.sub(r"[*_`~]+", "", text)
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def adaptive_padding_for_sentence(sentence: str) -> float:
    """Compute smart padding between sentences based on sentence length (words)."""
    if not sentence:
        return MIN_PADDING
    words = len(sentence.split())
    # base 20ms + 10ms per 5 words approximately, clamped to max
    pad = MIN_PADDING + 0.01 * min(words / 5.0, (MAX_PADDING - MIN_PADDING) / 0.01)
    return max(MIN_PADDING, min(MAX_PADDING, pad))

# -----------------------
# Azure TTS (blocking, run in thread)
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
# Pipeline helpers
# -----------------------
def ensure_structs(session: str):
    if session not in tts_sentence_queue:
        tts_sentence_queue[session] = []
    if session not in tts_gen_tasks:
        tts_gen_tasks[session] = []
    if session not in tts_cancel_events:
        tts_cancel_events[session] = asyncio.Event()
    if session not in assistant_is_speaking:
        assistant_is_speaking[session] = False

def cancel_tts_generation(session: str):
    """Cancel outstanding TTS generation tasks and clear their queues."""
    ev = tts_cancel_events.get(session)
    if ev:
        ev.set()
    tasks = tts_gen_tasks.get(session, []) or []
    for t in tasks:
        if not t.done():
            try:
                t.cancel()
            except:
                pass
    tts_sentence_queue.pop(session, None)
    tts_gen_tasks.pop(session, None)

async def _gen_audio_task(text: str) -> bytes:
    return await asyncio.to_thread(azure_tts_generate_sync, text)

def enqueue_sentence_for_pre_generation(session: str, sentence: str):
    """Start pre-generation for a sentence and ensure playback worker exists."""
    if not sentence or not sentence.strip():
        return
    ensure_structs(session)
    clean = clean_sentence_for_tts(sentence)
    if not clean:
        return
    # push queue
    tts_sentence_queue[session].append(clean)
    # start generation task immediately
    task = asyncio.create_task(_gen_audio_task(clean))
    tts_gen_tasks[session].append(task)
    # ensure worker
    if session not in tts_playback_task or tts_playback_task[session].done():
        tts_playback_task[session] = asyncio.create_task(tts_playback_worker(session))

# -----------------------
# Playback worker (sequential) - ensures no overlap
# -----------------------
async def tts_playback_worker(session: str):
    ws = playback_ws_registry.get(session)
    if not ws:
        return

    print(f"▶️ Playback worker started for {session}")
    tts_worker_running[session] = True
    ensure_structs(session)
    try:
        while True:
            # If cancel requested -> break
            if tts_cancel_events.get(session) and tts_cancel_events[session].is_set():
                print("🔇 Playback worker detected cancel - exiting")
                break

            gen_tasks = tts_gen_tasks.get(session, [])
            if not gen_tasks:
                if not tts_sentence_queue.get(session):
                    break
                await asyncio.sleep(0.01)
                continue

            gen_task = gen_tasks.pop(0)
            sentence_text = None
            if tts_sentence_queue.get(session):
                sentence_text = tts_sentence_queue[session].pop(0)

            if tts_cancel_events.get(session) and tts_cancel_events[session].is_set():
                try:
                    if not gen_task.done():
                        gen_task.cancel()
                except:
                    pass
                break

            # Reveal sentence text in UI only for voice-originated session
            if sentence_text and user_last_input_was_voice.get(session, False):
                try:
                    await ws.send_json({"type": "sentence_start", "text": sentence_text})
                except Exception:
                    print("❌ Could not send sentence_start - client disconnected.")
                    break

            # Await audio bytes for the sentence
            audio_bytes = b""
            try:
                audio_bytes = await gen_task
            except asyncio.CancelledError:
                print("🔇 Gen task cancelled mid-generation")
                continue
            except Exception as e:
                print("❌ Gen task exception:", e)
                audio_bytes = b""

            if tts_cancel_events.get(session) and tts_cancel_events[session].is_set():
                print("🔇 Dropping audio because cancel was set after generation")
                continue

            # Mark assistant as speaking
            assistant_is_speaking[session] = True

            # Send audio bytes
            if audio_bytes:
                try:
                    await ws.send_bytes(audio_bytes)
                except Exception as e:
                    print("❌ Failed to send audio bytes:", e)
                    assistant_is_speaking[session] = False
                    break

                # estimate duration and sleep with adaptive padding
                byte_len = len(audio_bytes)
                duration = byte_len / (SAMPLE_RATE * BYTES_PER_SAMPLE)
                padding = adaptive_padding_for_sentence(sentence_text or "")
                try:
                    await asyncio.sleep(duration + padding)
                except asyncio.CancelledError:
                    print("🔇 Playback sleep cancelled")
                    assistant_is_speaking[session] = False
                    break
            else:
                await asyncio.sleep(MIN_PADDING)

            assistant_is_speaking[session] = False

        # notify client done
        try:
            await ws.send_json({"type": "voice_done"})
        except:
            pass

    finally:
        tts_worker_running[session] = False
        # cleanup
        tts_sentence_queue.pop(session, None)
        remaining = tts_gen_tasks.pop(session, []) or []
        for t in remaining:
            try:
                if not t.done():
                    t.cancel()
            except:
                pass
        tts_cancel_events.pop(session, None)
        tts_playback_task.pop(session, None)
        playback_ws_registry.pop(session, None)
        assistant_is_speaking.pop(session, None)
        print(f"⏹ Playback worker finished for {session}")

# -----------------------
# Typed text handler (no TTS)
# -----------------------
async def handle_text_message(ws: WebSocket, text: str, session: str):
    text = (text or "").strip()
    if not text:
        print("⚠ Ignoring empty typed message")
        return

    print("📝 Text message:", text)
    user_last_input_was_voice[session] = False
    sentence_buffer[session] = ""
    await ws.send_json({"type": "final", "text": text})
    llm_stop_flags[session] = False

    async for token in stream_llm(text):
        if llm_stop_flags.get(session):
            print("⛔ Text LLM interrupted")
            break
        await ws.send_json({"type": "llm_stream", "token": token})

    await ws.send_json({"type": "llm_done"})
    print("✨ Text LLM done.")

# -----------------------
# STT + LLM + TTS main
# -----------------------
async def azure_stream(ws: WebSocket):
    session = str(uuid.uuid4())
    print("WS connected:", session)

    llm_stop_flags[session] = False
    user_last_input_was_voice[session] = False
    sentence_buffer[session] = ""
    ensure_structs(session)
    playback_ws_registry[session] = ws

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

    # partial STT callback - only used for showing partials, barge-in only if assistant is speaking
    def recognizing(evt):
        text = (evt.result.text or "").strip()
        if text and text not in ["", ".", "uh", "um"]:
            # Always send partial for UI (frontend ignores it normally)
            asyncio.run_coroutine_threadsafe(ws.send_json({"type": "partial", "text": text}), loop)

            # Only treat as barge-in if assistant is currently speaking
            if assistant_is_speaking.get(session, False):
                print("🔇 BARGE-IN detected while assistant speaking -> cancelling playback & generation")
                llm_stop_flags[session] = True
                if session in tts_cancel_events:
                    tts_cancel_events[session].set()
                cancel_tts_generation(session)
                # cancel playback worker if running
                task = tts_playback_task.get(session)
                if task and not task.done():
                    try:
                        task.cancel()
                    except:
                        pass
                # reset assistant speaking state
                assistant_is_speaking[session] = False
                # notify client to stop immediately
                asyncio.run_coroutine_threadsafe(ws.send_json({"type": "stop_all"}), loop)

    # final STT callback
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
            sentence_buffer[session] = ""
            try:
                async for token in stream_llm(text):
                    if llm_stop_flags.get(session):
                        print("⛔ STT LLM interrupted")
                        break

                    sentence_buffer[session] += token
                    buff = sentence_buffer[session].strip()
                    if not buff:
                        continue

                    if buff.endswith((".", "!", "?", "...")) or len(buff.split()) >= 60:
                        enqueue_sentence_for_pre_generation(session, buff)
                        sentence_buffer[session] = ""

            except Exception as e:
                await ws.send_json({"type": "llm_stream", "token": f"[LLM ERROR] {e}"})
                print("❌ LLM stream error:", e)

            leftover = sentence_buffer.get(session, "").strip()
            if leftover:
                enqueue_sentence_for_pre_generation(session, leftover)
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
                        asyncio.create_task(handle_text_message(ws, data["text"], session))
                        continue

                    if data.get("type") == "stop_llm":
                        # manual stop -> cancel generation & playback worker and reset state so voice can recover
                        print("⛔ Manual STOP received for session", session)
                        llm_stop_flags[session] = True
                        if session in tts_cancel_events:
                            tts_cancel_events[session].set()
                        cancel_tts_generation(session)
                        task = tts_playback_task.get(session)
                        if task and not task.done():
                            try:
                                task.cancel()
                            except:
                                pass
                        # create a fresh cancel_event so future TTS can run
                        tts_cancel_events[session] = asyncio.Event()
                        assistant_is_speaking[session] = False
                        # notify client to stop immediately
                        await ws.send_json({"type": "stop_all"})
                        continue

            # Raw PCM audio -> push to STT
            if msg.get("bytes"):
                try:
                    push_stream.write(msg["bytes"])
                except Exception:
                    pass

    finally:
        print("🟡 Cleaning WS session", session)
        try: push_stream.close()
        except: pass
        try: recognizer.stop_continuous_recognition()
        except: pass

        cancel_tts_generation(session)
        task = tts_playback_task.get(session)
        if task and not task.done():
            try:
                task.cancel()
            except:
                pass

        # cleanup dictionaries
        llm_stop_flags.pop(session, None)
        user_last_input_was_voice.pop(session, None)
        sentence_buffer.pop(session, None)
        tts_sentence_queue.pop(session, None)
        tts_gen_tasks.pop(session, None)
        tts_playback_task.pop(session, None)
        tts_cancel_events.pop(session, None)
        tts_worker_running.pop(session, None)
        playback_ws_registry.pop(session, None)
        assistant_is_speaking.pop(session, None)

        print("WS closed:", session)

@app.websocket("/ws/stream")
async def websocket_stream(ws: WebSocket):
    await ws.accept()
    await azure_stream(ws)
