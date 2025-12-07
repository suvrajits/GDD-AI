# stream_engine.py
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
llm_stop_flags = {}               # session -> bool
tts_sentence_queue = {}           # session -> [sentence_text]
tts_gen_tasks = {}                # session -> [asyncio.Task]
tts_cancel_events = {}            # session -> asyncio.Event
tts_playback_task = {}            # session -> asyncio.Task
playback_ws_registry = {}         # session -> WebSocket
assistant_is_speaking = {}        # session -> bool

gdd_wizard_active = {}            # session -> bool
gdd_wizard_stage = {}             # session -> int
gdd_session_map = {}              # session -> backend session id
llm_busy = {}   # session → bool
llm_busy.setdefault(session, False)

# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------
def ensure_structs(session: str):
    tts_sentence_queue.setdefault(session, [])
    tts_gen_tasks.setdefault(session, [])
    tts_cancel_events.setdefault(session, asyncio.Event())
    assistant_is_speaking.setdefault(session, False)
    llm_stop_flags.setdefault(session, False)
    gdd_wizard_active.setdefault(session, False)
    gdd_wizard_stage.setdefault(session, 0)

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

def cancel_tts_generation(session: str):
    ev = tts_cancel_events.get(session)
    if ev:
        ev.set()
    # cancel tasks
    for t in list(tts_gen_tasks.get(session, [])):
        if t and not t.done():
            try:
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
    A sentence is considered complete when a sentence-ending punctuation
    (.?!… ) appears with whitespace or end-of-buffer after it.
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
# TTS generation (blocking Azure TTS via SDK wrapped in thread)
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
    ws = playback_ws_registry.get(session)
    if not ws:
        return

    print(f"▶ Playback worker started for {session}")
    ensure_structs(session)

    try:
        while True:
            if tts_cancel_events[session].is_set():
                break

            if not tts_gen_tasks[session]:
                if not tts_sentence_queue[session]:
                    break
                await asyncio.sleep(0.01)
                continue

            gen_task = tts_gen_tasks[session].pop(0)
            sentence_text = tts_sentence_queue[session].pop(0) if tts_sentence_queue[session] else ""

            if tts_cancel_events[session].is_set():
                try:
                    gen_task.cancel()
                except:
                    pass
                break

            # notify frontend UI that a sentence is about to play
            if sentence_text:
                try:
                    await ws.send_json({"type": "sentence_start", "text": sentence_text})
                except:
                    pass

            try:
                audio_bytes = await gen_task
            except asyncio.CancelledError:
                audio_bytes = b""
            except Exception:
                audio_bytes = b""

            if not audio_bytes:
                continue

            assistant_is_speaking[session] = True
            try:
                # send raw PCM bytes
                await ws.send_bytes(audio_bytes)
            except Exception:
                assistant_is_speaking[session] = False
                break

            # wait for audio duration + a small padding
            duration = len(audio_bytes) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
            await asyncio.sleep(duration + adaptive_padding(sentence_text))

            assistant_is_speaking[session] = False

        try:
            await ws.send_json({"type": "voice_done"})
        except:
            pass

    finally:
        tts_sentence_queue[session] = []
        tts_gen_tasks[session] = []
        tts_cancel_events[session] = asyncio.Event()
        assistant_is_speaking[session] = False
        print(f"⏹ Playback finished for {session}")

# ------------------------------------------------------------------
# Enqueue TTS generation for a sentence (non-blocking)
# ------------------------------------------------------------------
def enqueue_sentence_for_tts(session: str, sentence: str):
    ensure_structs(session)
    if not sentence:
        return
    task = asyncio.create_task(async_tts(sentence))
    tts_gen_tasks[session].append(task)
    tts_sentence_queue[session].append(sentence)
    # ensure playback worker running
    if not tts_playback_task.get(session) or tts_playback_task[session].done():
        tts_playback_task[session] = asyncio.create_task(tts_playback_worker(session))

# ------------------------------------------------------------------
# LLM → sentences streaming helper (used by both text and voice paths)
# ------------------------------------------------------------------
async def stream_llm_to_client(ws: WebSocket, session: str, user_text: str):
    """
    Stream LLM tokens, extract sentences, send sentence events and enqueue TTS.
    Honors llm_stop_flags[session] — if set, stops streaming.
    """
    ensure_structs(session)
    llm_stop_flags[session] = False
    token_buffer = ""

    try:
        async for token in stream_llm(user_text):
            if llm_stop_flags.get(session):
                break

            # emit token (still send to UI for diagnostics, but UI ignores token stream UI-level)
            try:
                await ws.send_json({"type": "llm_stream", "token": token})
            except:
                pass

            token_buffer += token
            sentences, token_buffer = extract_sentences(token_buffer)

            for s in sentences:
                # publish sentence event to UI
                try:
                    await ws.send_json({"type": "llm_sentence", "sentence": s})
                except:
                    pass
                # enqueue TTS generation
                enqueue_sentence_for_tts(session, clean_sentence_for_tts(s))

    except Exception as e:
        print("stream_llm_to_client error:", e)
        try:
            await ws.send_json({"type": "llm_stream", "token": f"[ERR] {e}"})
        except:
            pass

    # leftover
    if token_buffer.strip():
        rem = token_buffer.strip()
        try:
            await ws.send_json({"type": "llm_sentence", "sentence": rem})
        except:
            pass
        enqueue_sentence_for_tts(session, clean_sentence_for_tts(rem))

    try:
        await ws.send_json({"type": "llm_done"})
    except:
        pass

# ------------------------------------------------------------------
# Unified GDD Wizard handler (W1B) — supports both text + voice
# Returns True if wizard handled the message (no LLM call)
# ------------------------------------------------------------------
async def process_gdd_wizard(ws: WebSocket, session: str, raw_text: str) -> bool:
    """
    Handles activation, go next, finish, answer saving, export.
    This single handler is used by both text & voice final messages before LLM.
    """
    ensure_structs(session)
    lower = raw_text.lower().strip()
    normalized = lower.replace(".", "").replace("?", "").replace("!", "")
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.replace("g d d", "gdd").replace("g d", "gd").strip()

    # activation phrases
    activation_phrases = [
        "activate gdd wizard", "activate gd wizard", "activate the gdd wizard",
        "start gdd wizard", "start the gdd wizard", "open gdd wizard",
        "launch gdd wizard", "activate wizard", "start wizard"
    ]

    # ---------- ACTIVATE ----------
    if any(p in normalized for p in activation_phrases):
        # load questions lazily (if present)
        try:
            from app.gdd_engine.gdd_questions import QUESTIONS
        except Exception:
            try:
                from gdd_engine.gdd_questions import QUESTIONS
            except:
                QUESTIONS = []

        gdd_wizard_active[session] = True
        gdd_wizard_stage[session] = 0

        # create backend gdd session (best-effort non-blocking)
        async def _start_gdd_session():
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    res = await client.post("http://localhost:8000/gdd/start")
                if res.status_code == 200:
                    j = res.json()
                    gdd_session_map[session] = j.get("session_id")
                    print("📡 Created gdd session:", gdd_session_map[session])
                    try:
                        await ws.send_json({"type": "gdd_session_id", "session_id": gdd_session_map[session]})
                    except:
                        pass
                else:
                    print("⚠ /gdd/start failed:", res.status_code, res.text)
            except Exception as e:
                print("❌ Exception calling /gdd/start:", e)

        asyncio.create_task(_start_gdd_session())

        # notify UI and send first question text & voice flag
        try:
            await ws.send_json({"type": "wizard_notice", "text": "🎮 **GDD Wizard Activated!** Say *Go Next* anytime."})
        except:
            pass

        try:
            if QUESTIONS:
                await ws.send_json({"type": "wizard_question", "text": QUESTIONS[0], "voice": QUESTIONS[0]})
                # queue first question TTS
                cleaned = clean_sentence_for_tts(QUESTIONS[0])
                if cleaned:
                    tts_sentence_queue[session].append(cleaned)
                    tts_gen_tasks[session].append(asyncio.create_task(async_tts(cleaned)))
                    if session not in tts_playback_task or tts_playback_task[session].done():
                        tts_playback_task[session] = asyncio.create_task(tts_playback_worker(session))
        except:
            pass

        # echo recognized text in UI transcript
        try:
            await ws.send_json({"type": "final", "text": raw_text})
        except:
            pass

        return True

    # ---------- GO NEXT ----------
    if gdd_wizard_active.get(session, False) and ("go next" in normalized or normalized == "next"):
        try:
            from app.gdd_engine.gdd_questions import QUESTIONS
        except Exception:
            try:
                from gdd_engine.gdd_questions import QUESTIONS
            except:
                QUESTIONS = []

        stage = gdd_wizard_stage.get(session, 0) + 1
        if stage >= len(QUESTIONS):
            try:
                await ws.send_json({"type": "wizard_notice", "text": "🎉 All questions answered! Say **Finish GDD**."})
            except:
                pass
            return True

        gdd_wizard_stage[session] = stage
        try:
            await ws.send_json({"type": "wizard_question", "text": QUESTIONS[stage], "voice": QUESTIONS[stage]})
        except:
            pass

        # queue TTS
        cleaned = clean_sentence_for_tts(QUESTIONS[stage])
        if cleaned:
            tts_sentence_queue[session].append(cleaned)
            tts_gen_tasks[session].append(asyncio.create_task(async_tts(cleaned)))
            if session not in tts_playback_task or tts_playback_task[session].done():
                tts_playback_task[session] = asyncio.create_task(tts_playback_worker(session))
        return True

    # ---------- FINISH GDD ----------
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
                    # publish results as final message
                    await ws.send_json({"type": "final", "text": data.get("markdown", "")})
                else:
                    await ws.send_json({"type": "wizard_notice", "text": f"❌ Error generating GDD (status {res.status_code})."})
            except Exception as e:
                print("❌ ERROR inside _finish():", e)
                try:
                    await ws.send_json({"type": "wizard_notice", "text": f"❌ Exception generating GDD: {e}"})
                except:
                    pass
            finally:
                gdd_wizard_active[session] = False

        asyncio.create_task(_finish())
        return True

    # ---------- EXPORT GDD ----------
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

    # ---------- ANSWER INSIDE WIZARD ----------
    if gdd_wizard_active.get(session, False):
        # Ignore short noisy fragments
        if len(raw_text.split()) < 3:
            # Not saving, not LLM
            return True

        # Save answer to backend session (best-effort)
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

    # Azure push stream for PCM 16kHz 16bit mono
    push_stream = speechsdk.audio.PushAudioInputStream(
        stream_format=speechsdk.audio.AudioStreamFormat(samples_per_second=SAMPLE_RATE, bits_per_sample=16, channels=1)
    )

    speech_config = speechsdk.SpeechConfig(subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION)
    recognizer = speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=speechsdk.audio.AudioConfig(stream=push_stream))

    loop = asyncio.get_event_loop()

    # PARTIAL STT: forward partials to client and detect interruptions
    def on_partial(evt):
        text = (evt.result.text or "").strip()
        if text:
            try:
                asyncio.run_coroutine_threadsafe(ws.send_json({"type": "partial", "text": text}), loop)
            except:
                pass

        # If assistant speaking, treat partial as interrupt
        if assistant_is_speaking.get(session, False) and text not in ("", ".", "uh", "um"):
            llm_stop_flags[session] = True
            ev = tts_cancel_events.get(session)
            if ev:
                ev.set()
            cancel_tts_generation(session)
            worker = tts_playback_task.get(session)
            if worker and not worker.done():
                try:
                    worker.cancel()
                except:
                    pass
            assistant_is_speaking[session] = False
            try:
                asyncio.run_coroutine_threadsafe(ws.send_json({"type": "stop_all"}), loop)
            except:
                pass

    # FINAL STT: use unified wizard handler and then LLM if not wizard
    def on_final(evt):
        try:
            if evt.result.reason != speechsdk.ResultReason.RecognizedSpeech:
                return
            raw_text = evt.result.text.strip()
            if not raw_text or raw_text in [".", "uh", "um"]:
                return

            print("🟢 Final STT:", raw_text)
            # first, try unified wizard handling
            fut = asyncio.run_coroutine_threadsafe(process_gdd_wizard(ws, session, raw_text), loop)
            handled = False
            try:
                handled = fut.result(timeout=10)
            except Exception:
                handled = False

            if handled:
                # wizard already handled; nothing else to do
                return

            # Not wizard — route through the standard text handler (which sends final + LLM)
            # We call the text-path handler below as a coroutine
            asyncio.run_coroutine_threadsafe(handle_text_message(ws, raw_text, session), loop)

        except Exception as e:
            print("on_final error:", e)
            traceback.print_exc()

    recognizer.recognizing.connect(on_partial)
    recognizer.recognized.connect(on_final)
    recognizer.start_continuous_recognition_async().get()
    print("🎤 Azure STT started successfully")

    # Main websocket receive loop: handles 'text' messages (typed) and stop commands
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break

            # typed/text payloads come as JSON
            if msg.get("text"):
                try:
                    data = json.loads(msg["text"])
                except Exception:
                    data = None

                if data:
                    if data.get("type") == "text":
                        # For text inputs use unified wizard handler first (synchronous await)
                        handled = await process_gdd_wizard(ws, session, data.get("text", ""))
                        if handled:
                            continue
                        # not wizard -> handle text LLM
                        asyncio.create_task(handle_text_message(ws, data.get("text", ""), session))
                        continue

                    if data.get("type") == "stop_llm":
                        # stop everything
                        llm_stop_flags[session] = True
                        ev = tts_cancel_events.get(session)
                        if ev:
                            ev.set()
                        cancel_tts_generation(session)
                        worker = tts_playback_task.get(session)
                        if worker and not worker.done():
                            try:
                                worker.cancel()
                            except:
                                pass
                        # reset playback events
                        tts_cancel_events[session] = asyncio.Event()
                        assistant_is_speaking[session] = False
                        try:
                            await ws.send_json({"type": "stop_all"})
                        except:
                            pass
                        continue

            # binary bytes (mic chunks) -> forward to Azure push stream
            if msg.get("bytes"):
                try:
                    push_stream.write(msg["bytes"])
                except Exception:
                    pass

    finally:
        # cleanup
        try:
            push_stream.close()
        except:
            pass
        try:
            recognizer.stop_continuous_recognition()
        except:
            pass

        cancel_tts_generation(session)
        worker = tts_playback_task.get(session)
        if worker and not worker.done():
            try:
                worker.cancel()
            except:
                pass

        # remove session keys if present
        for d in [llm_stop_flags, tts_sentence_queue, tts_gen_tasks, tts_cancel_events,
                  tts_playback_task, playback_ws_registry, assistant_is_speaking,
                  gdd_wizard_active, gdd_wizard_stage, gdd_session_map]:
            try:
                d.pop(session, None)
            except:
                pass

        print("WS closed:", session)

# ------------------------------------------------------------------
# Text message handler — TEXT path (typed messages)
# - Uses the same wizard handler (process_gdd_wizard) and if not handled,
#   streams to LLM -> sentences -> enqueues TTS + playback worker
# ------------------------------------------------------------------
async def handle_text_message(ws, text, session):
    text = (text or "").strip()
    if not text:
        return

    ensure_structs(session)

    # Wizard first
    handled = await process_gdd_wizard(ws, session, text)
    if handled:
        return

    # ❌ Prevent duplicate LLM calls
    if llm_busy.get(session):
        print("LLM already running — skip duplicate call")
        return

    # mark busy
    llm_busy[session] = True

    # echo
    await ws.send_json({"type": "final", "text": text})

    # run llm stream
    asyncio.create_task(stream_llm_to_client(ws, session, text))
