# backend/app/stream_engine.py
"""
Unified, production-ready stream_engine.py
- Sentence-level LLM streaming -> enqueue TTS -> playback worker
- Unified GDD wizard handler for text & voice
- Robust interrupt / stop handling
- No duplicate LLM runs (llm_busy)
"""

import uuid
import json
import asyncio
import re
import traceback
import httpx
import azure.cognitiveservices.speech as speechsdk

from fastapi import WebSocket

from .config import CONFIG
from .llm_orchestrator import stream_llm

# ------------------------------------------------------------------
# Configuration & constants
# ------------------------------------------------------------------
AZURE_SPEECH_KEY = CONFIG.get("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION = CONFIG.get("AZURE_SPEECH_REGION")

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
MIN_PADDING = 0.02
MAX_PADDING = 0.08

# ------------------------------------------------------------------
# Per-session state (isolated inside this module)
# ------------------------------------------------------------------
llm_stop_flags = {}            # session -> bool (stop LLM)
tts_sentence_queue = {}        # session -> [sentence_text]
tts_gen_tasks = {}             # session -> [asyncio.Task]
tts_cancel_events = {}         # session -> asyncio.Event
tts_playback_task = {}         # session -> asyncio.Task
playback_ws_registry = {}      # session -> WebSocket
assistant_is_speaking = {}     # session -> bool

gdd_wizard_active = {}         # session -> bool
gdd_wizard_stage = {}          # session -> int
gdd_session_map = {}           # session -> backend session id

llm_busy = {}                  # session -> bool (prevent duplicate LLM runs)

# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------
def ensure_structs(session: str):
    """Ensure per-session data structures exist."""
    tts_sentence_queue.setdefault(session, [])
    tts_gen_tasks.setdefault(session, [])
    tts_cancel_events.setdefault(session, asyncio.Event())
    tts_playback_task.setdefault(session, None)
    playback_ws_registry.setdefault(session, None)
    assistant_is_speaking.setdefault(session, False)
    llm_stop_flags.setdefault(session, False)
    gdd_wizard_active.setdefault(session, False)
    gdd_wizard_stage.setdefault(session, 0)
    gdd_session_map.setdefault(session, None)
    llm_busy.setdefault(session, False)

def cleanup_session(session: str):
    """Remove session data (best-effort)."""
    for d in [llm_stop_flags, tts_sentence_queue, tts_gen_tasks, tts_cancel_events,
              tts_playback_task, playback_ws_registry, assistant_is_speaking,
              gdd_wizard_active, gdd_wizard_stage, gdd_session_map, llm_busy]:
        try:
            d.pop(session, None)
        except Exception:
            pass

def clean_sentence_for_tts(text: str) -> str:
    """Light cleaning to avoid TTS choking on markdown or weird characters."""
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

def cancel_tts_generation(session: str):
    """Signal generator tasks to cancel and clear queues."""
    ev = tts_cancel_events.get(session)
    if ev:
        ev.set()
    for t in list(tts_gen_tasks.get(session, []) or []):
        try:
            if t and not t.done():
                t.cancel()
        except Exception:
            pass
    tts_sentence_queue[session] = []
    tts_gen_tasks[session] = []

# ------------------------------------------------------------------
# Sentence extractor for streaming tokens -> sentences
# ------------------------------------------------------------------
def extract_sentences(buffer: str):
    """
    Return (list_of_complete_sentences, remainder)
    A sentence is considered complete when an ending punctuation
    (.!?…) appears followed by whitespace or end-of-buffer.
    """
    sentences = []
    buf = buffer
    last_cut = 0
    ends = set(".!?…")
    for idx, ch in enumerate(buf):
        if ch in ends:
            next_char = buf[idx + 1] if idx + 1 < len(buf) else None
            if (next_char is None) or (next_char.isspace()):
                s = buf[last_cut: idx + 1].strip()
                if s:
                    sentences.append(s)
                last_cut = idx + 1
    remainder = buf[last_cut:].lstrip()
    return sentences, remainder

# ------------------------------------------------------------------
# Azure TTS helpers (synchronous SDK call wrapped with to_thread)
# ------------------------------------------------------------------
speech_tts_config = speechsdk.SpeechConfig(
    subscription=AZURE_SPEECH_KEY,
    region=AZURE_SPEECH_REGION
)
speech_tts_config.speech_synthesis_voice_name = "en-IN-NeerjaNeural"
speech_tts_config.set_speech_synthesis_output_format(
    speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm
)

def azure_tts_generate_sync(text: str) -> bytes:
    """Blocking call to Azure TTS SDK - returns raw PCM bytes (16kHz 16-bit mono)."""
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_tts_config, audio_config=None
    )
    result = synthesizer.speak_text_async(text).get()
    if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
        return result.audio_data
    print("❌ Azure TTS error:", result.reason)
    return b""

async def async_tts(text: str) -> bytes:
    return await asyncio.to_thread(azure_tts_generate_sync, text)

# ------------------------------------------------------------------
# Playback worker — dequeues TTS tasks, streams PCM to websocket
# ------------------------------------------------------------------
async def tts_playback_worker(session: str):
    """Worker that takes pre-generated TTS tasks and streams PCM to client websocket.
    Each sentence enqueued results in:
      - ws.json({"type":"sentence_start","text": sentence_text})
      - ws.send_bytes(pcm_bytes)
      - sleep(duration + padding)
    """
    ws = playback_ws_registry.get(session)
    if not ws:
        return

    print(f"▶ Playback worker started for {session}")
    ensure_structs(session)

    try:
        while True:
            # cancellation requested
            if tts_cancel_events[session].is_set():
                break

            # if no generation tasks yet, but queue is empty -> finish
            if not tts_gen_tasks[session]:
                if not tts_sentence_queue[session]:
                    break
                await asyncio.sleep(0.01)
                continue

            gen_task = tts_gen_tasks[session].pop(0)
            item = tts_sentence_queue[session].pop(0)
            if isinstance(item, tuple):
                sentence_text, source = item
            else:
                sentence_text, source = item, "llm"


            # if cancellation occurred after pop
            if tts_cancel_events[session].is_set():
                try:
                    gen_task.cancel()
                except Exception:
                    pass
                break

            # notify frontend about upcoming sentence (UI sync)
            # Only UI-sync wizard questions; LLM sentences already shown
            if source == "wizard" and sentence_text:
                try:
                    await ws.send_json({"type": "sentence_start", "text": sentence_text})
                except:
                    pass


            # await audio generation result
            try:
                audio_bytes = await gen_task
            except asyncio.CancelledError:
                audio_bytes = b""
            except Exception:
                audio_bytes = b""

            if not audio_bytes:
                continue

            # stream bytes
            assistant_is_speaking[session] = True
            try:
                await ws.send_bytes(audio_bytes)
            except Exception:
                assistant_is_speaking[session] = False
                break

            # sleep for duration + adaptive padding
            duration = len(audio_bytes) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
            await asyncio.sleep(duration + adaptive_padding(sentence_text))

            assistant_is_speaking[session] = False

        # finished: signal voice_done
        try:
            await ws.send_json({"type": "voice_done"})
        except Exception:
            pass

    finally:
        # reset per-session generation structures
        tts_sentence_queue[session] = []
        tts_gen_tasks[session] = []
        tts_cancel_events[session] = asyncio.Event()
        assistant_is_speaking[session] = False
        print(f"⏹ Playback finished for {session}")

# ------------------------------------------------------------------
# Enqueue TTS generation for a sentence (non-blocking)
# ------------------------------------------------------------------
def enqueue_sentence_for_tts(session: str, sentence: str, source="llm"):
    ensure_structs(session)
    if not sentence:
        return

    # mark each queued item with its source
    tts_sentence_queue[session].append((sentence, source))

    task = asyncio.create_task(async_tts(sentence))
    tts_gen_tasks[session].append(task)

    # ensure playback worker running
    if not tts_playback_task.get(session) or tts_playback_task[session].done():
        tts_playback_task[session] = asyncio.create_task(tts_playback_worker(session))

# ------------------------------------------------------------------
# LLM -> sentences streaming helper
# ------------------------------------------------------------------
async def stream_llm_to_client(ws: WebSocket, session: str, user_text: str):
    """
    Stream tokens from stream_llm(user_text), extract sentence-level pieces,
    send `llm_sentence` events and enqueue corresponding TTS generation.
    Sends `llm_stream` token events too (UI may ignore tokens).
    """
    ensure_structs(session)
    llm_stop_flags[session] = False
    token_buffer = ""

    try:
        async for token in stream_llm(user_text):
            if llm_stop_flags.get(session):
                print(f"[{session}] LLM stop flag set -> breaking stream")
                break

            # forward token (UI-level may ignore)
            try:
                await ws.send_json({"type": "llm_stream", "token": token})
            except Exception:
                pass

            token_buffer += token
            sentences, token_buffer = extract_sentences(token_buffer)

            for s in sentences:
                # publish sentence event to UI
                try:
                    await ws.send_json({"type": "llm_sentence", "sentence": s})
                except Exception:
                    pass
                # enqueue TTS generation (cleaned)
                enqueue_sentence_for_tts(session, clean_sentence_for_tts(s))

    except Exception as e:
        print("stream_llm_to_client error:", e)
        traceback.print_exc()
        try:
            await ws.send_json({"type": "llm_stream", "token": f"[ERR] {e}"})
        except Exception:
            pass

    # leftover
    if token_buffer.strip():
        rem = token_buffer.strip()
        try:
            await ws.send_json({"type": "llm_sentence", "sentence": rem})
        except Exception:
            pass
        enqueue_sentence_for_tts(session, clean_sentence_for_tts(rem))

    try:
        await ws.send_json({"type": "llm_done"})
    except Exception:
        pass

    # mark not busy (allow next calls)
    llm_busy[session] = False

# ------------------------------------------------------------------
# Unified GDD Wizard handler (single code path for text & voice)
# Returns True if wizard handled the message (no LLM call should follow)
# ------------------------------------------------------------------
async def process_gdd_wizard(ws: WebSocket, session: str, raw_text: str) -> bool:
    """
    Handles activation, go next, finish, answer saving, export.
    Called by both text & voice handlers before LLM invocation.
    """
    ensure_structs(session)

    lower = (raw_text or "").lower().strip()
    normalized = lower.replace(".", "").replace("?", "").replace("!", "")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = normalized.replace("g d d", "gdd").replace("g d", "gd")

    # activation phrases
    activation_phrases = [
        "activate gdd wizard", "activate gd wizard", "activate the gdd wizard",
        "start gdd wizard", "start the gdd wizard", "open gdd wizard",
        "launch gdd wizard", "activate wizard", "start wizard"
    ]

    # -------- ACTIVATE ----------
    if any(p in normalized for p in activation_phrases):
        # load QUESTIONS lazily
        try:
            from app.gdd_engine.gdd_questions import QUESTIONS
        except Exception:
            try:
                from gdd_engine.gdd_questions import QUESTIONS
            except Exception:
                QUESTIONS = []

        gdd_wizard_active[session] = True
        gdd_wizard_stage[session] = 0

        # create backend session (best-effort)
        async def _start():
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    res = await client.post("http://localhost:8000/gdd/start")
                if res.status_code == 200:
                    j = res.json()
                    gdd_session_map[session] = j.get("session_id")
                    print("📡 Created gdd session:", gdd_session_map[session])
                    try:
                        await ws.send_json({"type": "gdd_session_id", "session_id": gdd_session_map[session]})
                    except Exception:
                        pass
                else:
                    print("⚠ /gdd/start failed:", res.status_code, res.text)
            except Exception as e:
                print("❌ Exception calling /gdd/start:", e)

        asyncio.create_task(_start())

        # notify UI & send first question text+voice
        try:
            await ws.send_json({"type": "wizard_notice", "text": "🎮 **GDD Wizard Activated!** Say *Go Next* anytime."})
            if QUESTIONS:
                await ws.send_json({"type": "wizard_question", "text": QUESTIONS[0], "voice": QUESTIONS[0]})
                # queue first question TTS
                cleaned = clean_sentence_for_tts(QUESTIONS[0])
                if cleaned:
                    enqueue_sentence_for_tts(session, cleaned, source="wizard")
                    tts_gen_tasks[session].append(asyncio.create_task(async_tts(cleaned)))
                    if session not in tts_playback_task or (tts_playback_task[session] and tts_playback_task[session].done()):
                        tts_playback_task[session] = asyncio.create_task(tts_playback_worker(session))
        except Exception:
            pass

        # echo recognized text for transcript
        try:
            await ws.send_json({"type": "final", "text": raw_text})
        except Exception:
            pass

        return True

    # -------- GO NEXT ----------
    if gdd_wizard_active.get(session, False) and ("go next" in normalized or normalized == "next"):
        try:
            from app.gdd_engine.gdd_questions import QUESTIONS
        except Exception:
            try:
                from gdd_engine.gdd_questions import QUESTIONS
            except Exception:
                QUESTIONS = []

        stage = gdd_wizard_stage.get(session, 0) + 1
        if stage >= len(QUESTIONS):
            try:
                await ws.send_json({"type": "wizard_notice", "text": "🎉 All questions answered! Say **Finish GDD**."})
            except Exception:
                pass
            return True

        gdd_wizard_stage[session] = stage
        try:
            await ws.send_json({"type": "wizard_question", "text": QUESTIONS[stage], "voice": QUESTIONS[stage]})
        except Exception:
            pass

        # queue TTS for the next question
        cleaned = clean_sentence_for_tts(QUESTIONS[stage])
        if cleaned:
            enqueue_sentence_for_tts(session, cleaned, source="wizard")
            tts_gen_tasks[session].append(asyncio.create_task(async_tts(cleaned)))
            if session not in tts_playback_task or (tts_playback_task[session] and tts_playback_task[session].done()):
                tts_playback_task[session] = asyncio.create_task(tts_playback_worker(session))
        return True

    # -------- FINISH GDD ----------
    if gdd_wizard_active.get(session, False) and re.search(r"\b(finish gdd|generate gdd|complete gdd)\b", normalized):
        async def _finish():
            try:
                gdd_sid = gdd_session_map.get(session)
                if not gdd_sid:
                    await ws.send_json({"type": "wizard_notice", "text": "❌ No GDD session found — nothing to finish."})
                    gdd_wizard_active[session] = False
                    gdd_wizard_stage[session] = 0
                    return
                async with httpx.AsyncClient(timeout=20.0) as client:
                    res = await client.post("http://localhost:8000/gdd/finish", json={"session_id": gdd_sid})
                if res.status_code == 200:
                    data = res.json()
                    await ws.send_json({"type": "wizard_notice", "text": "📘 **Your GDD is ready! Say Download GDD to Download it**"})
                    await ws.send_json({"type": "final", "text": data.get("markdown", "")})
                else:
                    await ws.send_json({"type": "wizard_notice", "text": f"❌ Error generating GDD (status {res.status_code})."})
            except Exception as e:
                print("❌ ERROR inside _finish():", e)
                try:
                    await ws.send_json({"type": "wizard_notice", "text": f"❌ Exception generating GDD: {e}"})
                except Exception:
                    pass
            finally:
                gdd_wizard_active[session] = False

        asyncio.create_task(_finish())
        return True

    # -------- EXPORT GDD ----------
    if any(p in normalized for p in ("export gdd", "export the gdd", "download gdd", "export document")):
        gdd_sid = gdd_session_map.get(session)
        if not gdd_sid:
            await ws.send_json({"type": "wizard_notice", "text": "❌ No GDD available to export. Please finish GDD first."})
            return True

        async def _export():
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.post("http://localhost:8000/gdd/export", json={"session_id": gdd_sid})
                if res.status_code != 200:
                    await ws.send_json({"type": "wizard_notice", "text": f"❌ Export failed ({res.status_code})."})
                    return
                await ws.send_json({"type": "gdd_export_ready", "filename": f"GDD_{gdd_sid}.docx"})
            except Exception as e:
                print("❌ Export error:", e)
                try:
                    await ws.send_json({"type": "wizard_notice", "text": "❌ Export failed."})
                except:
                    pass

        asyncio.create_task(_export())
        return True

    # -------- SAVE ANSWER INSIDE WIZARD ----------
    if gdd_wizard_active.get(session, False):
        # ignore extremely short/noisy fragments
        if len(raw_text.split()) < 3:
            # not useful to save, but consume as handled
            return True

        async def _record_answer():
            try:
                gdd_sid = gdd_session_map.get(session)
                if not gdd_sid:
                    # best-effort: create backend session then save
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        res = await client.post("http://localhost:8000/gdd/start")
                        if res.status_code == 200:
                            j = res.json()
                            gdd_session_map[session] = j.get("session_id")
                            gdd_sid = gdd_session_map[session]
                if not gdd_sid:
                    return
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post("http://localhost:8000/gdd/answer", json={"session_id": gdd_sid, "answer": raw_text})
                await ws.send_json({"type": "wizard_answer", "text": raw_text})
            except Exception as e:
                print("❌ /gdd/answer failed:", e)

        asyncio.create_task(_record_answer())
        return True

    return False

# ------------------------------------------------------------------
# Main voice stream entrypoint (to be used by FastAPI websocket route)
# ------------------------------------------------------------------
async def azure_stream(ws: WebSocket):
    session = str(uuid.uuid4())
    print("WS connected:", session)
    ensure_structs(session)
    playback_ws_registry[session] = ws

    # create push stream for Azure Speech SDK
    push_stream = speechsdk.audio.PushAudioInputStream(
        stream_format=speechsdk.audio.AudioStreamFormat(samples_per_second=SAMPLE_RATE, bits_per_sample=16, channels=1)
    )

    speech_config = speechsdk.SpeechConfig(subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION)
    recognizer = speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=speechsdk.audio.AudioConfig(stream=push_stream))

    loop = asyncio.get_event_loop()

    # PARTIAL STT: forward partials to client and detect interruptions
    def on_partial(evt):
        try:
            text = (evt.result.text or "").strip()
            if text:
                try:
                    asyncio.run_coroutine_threadsafe(ws.send_json({"type": "partial", "text": text}), loop)
                except Exception:
                    pass

            # If assistant speaking, treat partial as interrupt
            if assistant_is_speaking.get(session, False) and text not in ("", ".", "uh", "um"):
                print(f"[{session}] Partial STT during speech -> interrupting")
                llm_stop_flags[session] = True
                ev = tts_cancel_events.get(session)
                if ev:
                    ev.set()
                cancel_tts_generation(session)
                worker = tts_playback_task.get(session)
                if worker and not worker.done():
                    try:
                        worker.cancel()
                    except Exception:
                        pass
                assistant_is_speaking[session] = False
                try:
                    asyncio.run_coroutine_threadsafe(ws.send_json({"type": "stop_all"}), loop)
                except Exception:
                    pass
        except Exception as e:
            print("on_partial error:", e)

    # FINAL STT: use unified wizard handler and then LLM if not handled
    def on_final(evt):
        try:
            if evt.result.reason != speechsdk.ResultReason.RecognizedSpeech:
                return
            raw_text = evt.result.text.strip()
            if not raw_text or raw_text in [".", "uh", "um"]:
                return

            print("🟢 Final STT:", raw_text)

            # first, attempt GDD wizard handling (synchronous call via thread-safe future)
            fut = asyncio.run_coroutine_threadsafe(process_gdd_wizard(ws, session, raw_text), loop)
            handled = False
            try:
                handled = fut.result(timeout=8)
            except Exception:
                handled = False

            if handled:
                # wizard handled message; nothing else to do
                return

            # Not wizard -> process as normal text message (which will trigger LLM stream)
            # We call the text handler on the loop
            # Only trigger LLM if not already busy
            if not llm_busy.get(session):
                asyncio.run_coroutine_threadsafe(handle_text_message(ws, raw_text, session), loop)
            else:
                print(f"[{session}] Voice ignored — LLM busy")


        except Exception as e:
            print("on_final error:", e)
            traceback.print_exc()

    recognizer.recognizing.connect(on_partial)
    recognizer.recognized.connect(on_final)
    recognizer.start_continuous_recognition_async().get()
    print("🎤 Azure STT started successfully")

    # Main websocket loop: handles typed text and stop commands and incoming audio bytes from client
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break

            if msg.get("text"):
                try:
                    data = json.loads(msg["text"])
                except Exception:
                    data = None

                if data:
                    # typed text message
                    if data.get("type") == "text":
                        handled = await process_gdd_wizard(ws, session, data.get("text", ""))
                        if handled:
                            continue
                        # if llm is busy, skip duplicate typed calls
                        if llm_busy.get(session):
                            print(f"[{session}] LLM busy - skip typed call")
                            continue
                        # mark busy and spawn llm stream
                        llm_busy[session] = True
                        # echo user text
                        await ws.send_json({"type": "final", "text": data.get("text", "")})
                        asyncio.create_task(stream_llm_to_client(ws, session, data.get("text", "")))
                        continue

                    if data.get("type") == "stop_llm":
                        # stop everything immediately
                        print(f"[{session}] STOP_LLm received -> cancelling")
                        llm_stop_flags[session] = True
                        ev = tts_cancel_events.get(session)
                        if ev:
                            ev.set()
                        cancel_tts_generation(session)
                        worker = tts_playback_task.get(session)
                        if worker and not worker.done():
                            try:
                                worker.cancel()
                            except Exception:
                                pass
                        # reset events
                        tts_cancel_events[session] = asyncio.Event()
                        assistant_is_speaking[session] = False
                        try:
                            await ws.send_json({"type": "stop_all"})
                        except Exception:
                            pass
                        # allow future llm calls
                        llm_busy[session] = False
                        continue

            # binary audio frames (mic PCM) forwarded to Azure push stream
            if msg.get("bytes"):
                try:
                    push_stream.write(msg["bytes"])
                except Exception:
                    pass

    finally:
        # cleanup
        try:
            push_stream.close()
        except Exception:
            pass

        try:
            recognizer.stop_continuous_recognition()
        except Exception:
            pass

        cancel_tts_generation(session)
        worker = tts_playback_task.get(session)
        if worker and not getattr(worker, "done", lambda: True)():
            try:
                worker.cancel()
            except Exception:
                pass

        cleanup_session(session)
        print("WS closed:", session)

# ------------------------------------------------------------------
# Text message handler — TEXT path (typed messages)
# ------------------------------------------------------------------
async def handle_text_message(ws, text, session):
    text = (text or "").strip()
    if not text:
        return

    ensure_structs(session)

    # 🚨 Block duplicate LLM calls IMMEDIATELY
    if llm_busy.get(session):
        print(f"[{session}] LLM BUSY → ignoring duplicate handle_text_message()")
        return

    # Mark busy BEFORE any async wizard or LLM logic
    llm_busy[session] = True

    # Wizard handling (may consume the message)
    handled = await process_gdd_wizard(ws, session, text)
    if handled:
        llm_busy[session] = False     # Wizard does NOT run LLM
        return

    # Echo user message
    await ws.send_json({"type": "final", "text": text})

    # Run LLM streaming
    asyncio.create_task(stream_llm_to_client(ws, session, text))
