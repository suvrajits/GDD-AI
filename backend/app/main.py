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
from app.gdd_api import router as gdd_router


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

# Wizard state
gdd_wizard_active: Dict[str, bool] = {}
gdd_wizard_stage: Dict[str, int] = {}
gdd_session_map: Dict[str, str] = {}
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
MIN_PADDING = 0.02
MAX_PADDING = 0.08

# -----------------------
# APP
# -----------------------
app = FastAPI()
static_path = os.path.join(os.path.dirname(__file__), "static")
app.include_router(rag_router)
app.mount("/static", StaticFiles(directory=static_path), name="static")
app.include_router(gdd_router, prefix="/gdd", tags=["GDD"])

AZURE_SPEECH_KEY = CONFIG["AZURE_SPEECH_KEY"]
AZURE_SPEECH_REGION = CONFIG["AZURE_SPEECH_REGION"]

print("🔐 Azure Speech Key Loaded:", AZURE_SPEECH_KEY[:5] + "****")
print("🌍 Region:", AZURE_SPEECH_REGION)

# -------------------------------------------------
# UTILS
# -------------------------------------------------
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


# -------------------------------------------------
# TTS
# -------------------------------------------------
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
        speech_config=speech_tts_config, audio_config=None
    )
    result = synthesizer.speak_text_async(text).get()
    if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
        return result.audio_data
    print("❌ Azure TTS error:", result.reason)
    return b""

async def async_tts(text: str) -> bytes:
    return await asyncio.to_thread(azure_tts_generate_sync, text)


# -------------------------------------------------
# STRUCT + CLEANUP HELPERS
# -------------------------------------------------
def ensure_structs(session: str):
    tts_sentence_queue.setdefault(session, [])
    tts_gen_tasks.setdefault(session, [])
    tts_cancel_events.setdefault(session, asyncio.Event())
    assistant_is_speaking.setdefault(session, False)

def cancel_tts_generation(session: str):
    ev = tts_cancel_events.get(session)
    if ev:
        ev.set()

    for t in tts_gen_tasks.get(session, []):
        if not t.done():
            try:
                t.cancel()
            except:
                pass

    tts_sentence_queue[session] = []
    tts_gen_tasks[session] = []


# -------------------------------------------------
# PLAYBACK WORKER
# -------------------------------------------------
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
                try: gen_task.cancel()
                except: pass
                break

            if sentence_text and user_last_input_was_voice.get(session, False):
                try:
                    await ws.send_json({"type": "sentence_start", "text": sentence_text})
                except:
                    break

            try:
                audio_bytes = await gen_task
            except:
                audio_bytes = b""

            if not audio_bytes:
                continue

            assistant_is_speaking[session] = True
            try:
                await ws.send_bytes(audio_bytes)
            except:
                assistant_is_speaking[session] = False
                break

            duration = len(audio_bytes) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
            await asyncio.sleep(duration + adaptive_padding(sentence_text))

            assistant_is_speaking[session] = False

        try: await ws.send_json({"type": "voice_done"})
        except: pass

    finally:
        tts_sentence_queue[session] = []
        tts_gen_tasks[session] = []
        tts_cancel_events[session] = asyncio.Event()
        assistant_is_speaking[session] = False
        print(f"⏹ Playback finished for {session}")


# -------------------------------------------------
# TEXT MESSAGE HANDLER (unchanged)
# -------------------------------------------------
async def handle_text_message(ws: WebSocket, text: str, session: str):
    text = text.strip()
    if not text:
        return

    # -----------------------------------------------------------------------------------
    # ⭐ NEW — Add TEXT-BASED WIZARD LOGIC (mirrors voice recognized() logic)
    # -----------------------------------------------------------------------------------
    lower = text.lower().strip()
    normalized = lower.replace(".", "").replace("?", "").replace("!", "")
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.replace("g d d", "gdd").replace("g d", "gd")

    # Same activation phrases used in voice path
    activation_phrases = [
        "activate gdd wizard", "activate gd wizard",
        "activate the gdd wizard", "start gdd wizard",
        "start the gdd wizard", "open gdd wizard",
        "launch gdd wizard", "activate wizard", "start wizard",
    ]

    # -----------------------------
    # 1) WIZARD ACTIVATION (TEXT)
    # -----------------------------
    if any(p in normalized for p in activation_phrases):
        from app.gdd_engine.gdd_questions import QUESTIONS

        gdd_wizard_active[session] = True
        gdd_wizard_stage[session] = 0

        await ws.send_json({
            "type": "wizard_notice",
            "text": "🎮 **GDD Wizard Activated!** Say *Go Next* anytime."
        })

        await ws.send_json({
            "type": "wizard_question",
            "text": f"{QUESTIONS[0]}"
        })


        return  # IMPORTANT: do not run LLM in wizard mode

    # -----------------------------
    # 2) GO NEXT (TEXT)
    # -----------------------------
    if gdd_wizard_active.get(session, False) and ("go next" in normalized or normalized == "next"):
        from app.gdd_engine.gdd_questions import QUESTIONS

        stage = gdd_wizard_stage.get(session, 0) + 1

        # If done with all questions, instruct user to finish
        if stage >= len(QUESTIONS):
            await ws.send_json({
                "type": "wizard_notice",
                "text": "🎉 All questions answered! Say **Finish GDD** to generate your document."
            })
            return

        gdd_wizard_stage[session] = stage

        await ws.send_json({
            "type": "wizard_question",
            "text": f"{QUESTIONS[stage]}"
        })


        return  # do not run LLM

    # -----------------------------
    # 3) ANSWER INSIDE WIZARD (TEXT)
    # -----------------------------
    if gdd_wizard_active.get(session, False):
        import httpx

        async with httpx.AsyncClient() as client:
            await client.post(
                "http://localhost:8000/gdd/answer",
                json={"session_id": session, "answer": text}
            )

        await ws.send_json({
            "type": "wizard_answer",
            "text": text
        })

        return  # remain in wizard; do not call LLM
    # -----------------------------------------------------------------------------------
    # END NEW WIZARD LOGIC
    # -----------------------------------------------------------------------------------

    # Normal LLM mode continues below
    await ws.send_json({"type": "final", "text": text})

    llm_stop_flags[session] = False

    async for token in stream_llm(text):
        if llm_stop_flags[session]:
            break
        await ws.send_json({"type": "llm_stream", "token": token})

    await ws.send_json({"type": "llm_done"})



# -------------------------------------------------
# MAIN STREAM
# -------------------------------------------------
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
            samples_per_second=SAMPLE_RATE, bits_per_sample=16, channels=1
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

    # -------------------------------
    # PARTIAL STT
    # -------------------------------
    def recognizing(evt):
        text = (evt.result.text or "").strip()

        if text:
            asyncio.run_coroutine_threadsafe(
                ws.send_json({"type": "partial", "text": text}), loop
            )

        if assistant_is_speaking.get(session, False) and text not in ["", ".", "uh", "um"]:
            llm_stop_flags[session] = True
            if session in tts_cancel_events:
                tts_cancel_events[session].set()
            cancel_tts_generation(session)
            worker = tts_playback_task.get(session)
            if worker and not worker.done():
                try: worker.cancel()
                except: pass
            assistant_is_speaking[session] = False
            asyncio.run_coroutine_threadsafe(ws.send_json({"type": "stop_all"}), loop)

    # -------------------------------
    # FINAL STT
    # -------------------------------
    def recognized(evt):

        if evt.result.reason != speechsdk.ResultReason.RecognizedSpeech:
            return

        raw_text = evt.result.text.strip()
        if not raw_text or raw_text in [".", "uh", "um"]:
            print("⚠ Ignoring garbage:", raw_text)
            return

        print("🟢 Final STT:", raw_text)
        # -----------------------------------------
        # Load QUESTIONS list (required for ALL wizard actions)
        # -----------------------------------------
        try:
            from app.gdd_engine.gdd_questions import QUESTIONS
        except Exception:
            try:
                from gdd_engine.gdd_questions import QUESTIONS
            except:
                QUESTIONS = []


        # Normalize STT:
        lower = raw_text.lower().strip()
        lower = lower.replace(".", "").replace("?", "").replace("!", "")
        lower = re.sub(r"\s+", " ", lower)

        # Fix Azure variants
        normalized = lower
        normalized = normalized.replace("g d d", "gdd")
        normalized = normalized.replace("g d", "gd")
        normalized = normalized.replace("g. d. d.", "gdd")
        normalized = normalized.strip()

        user_last_input_was_voice[session] = True
        sentence_buffer[session] = ""

        # ---------------------------------------------------------
        # 🔥 Robust Wizard Activation
        # ---------------------------------------------------------
        activation_phrases = [
            "activate gdd wizard",
            "activate gd wizard",
            "activate the gdd wizard",
            "start gdd wizard",
            "start the gdd wizard",
            "open gdd wizard",
            "launch gdd wizard",
            "activate wizard",
            "start wizard",
        ]

        # WIZARD ACTIVATION (VOICE) — UNIFIED WITH TEXT MODE
        # WIZARD ACTIVATION (voice) — create a backend GDD session and store it
        if any(re.search(rf"\b{re.escape(p)}\b", normalized) for p in activation_phrases):
            print("🎯 WIZARD ACTIVATED (voice):", normalized)
            gdd_wizard_active[session] = True
            gdd_wizard_stage[session] = 0

            # create server-side GDD session (non-blocking best-effort)
            async def _start_gdd_session():
                import httpx
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        res = await client.post("http://localhost:8000/gdd/start")
                    if res.status_code == 200:
                        j = res.json()
                        # store mapping for subsequent /gdd/answer and /gdd/finish
                        gdd_session_map[session] = j.get("session_id")
                        print("📡 Created gdd session:", gdd_session_map[session])
                    else:
                        print("⚠ /gdd/start failed:", res.status_code, res.text)
                except Exception as e:
                    print("❌ Exception calling /gdd/start:", e)

            # launch without blocking the recognizer (fire-and-forget)
            asyncio.run_coroutine_threadsafe(_start_gdd_session(), loop)

            # immediate UI feedback
            asyncio.run_coroutine_threadsafe(
                ws.send_json({"type": "wizard_notice",
                            "text": "🎮 **GDD Wizard Activated!**\nSay **Go Next** anytime to proceed."}),
                loop
            )
            # also send first question (we can still send question text while session is being created)
            if QUESTIONS:
                asyncio.run_coroutine_threadsafe(
                    ws.send_json({"type": "wizard_question",
                                "text": f"{QUESTIONS[0]}"}),
                    loop
                )
            # echo recognized text in UI transcript
            asyncio.run_coroutine_threadsafe(ws.send_json({"type": "final", "text": raw_text}), loop)
            return



        # ---------------------------------------------------------
        # Wizard Go Next
        # ---------------------------------------------------------
        # 2) Go Next
        if gdd_wizard_active.get(session, False) and ("go next" in normalized or normalized == "next"):
            stage = gdd_wizard_stage[session] + 1
            if stage >= len(QUESTIONS):
                asyncio.run_coroutine_threadsafe(
                    ws.send_json({
                        "type": "wizard_notice",
                        "text": "🎉 All questions answered! Say **Finish GDD**."
                    }),
                    loop
                )
                return

            gdd_wizard_stage[session] = stage
            asyncio.run_coroutine_threadsafe(
                ws.send_json({
                    "type": "wizard_question",
                    "text": f"{QUESTIONS[stage]}"
                }),
                loop
            )
            return


        # ⭐⭐⭐ 3) FINISH GDD — MUST BE BEFORE ANSWER ⭐⭐⭐
        # Finish GDD — use server-side session id
        if gdd_wizard_active.get(session, False) and re.search(r"\b(finish gdd|generate gdd|complete gdd)\b", normalized):
            print("🎯 FINISH GDD triggered:", normalized)

            async def _finish():
                import httpx
                try:
                    gdd_sid = gdd_session_map.get(session)
                    if not gdd_sid:
                        # nothing saved -> inform user
                        await ws.send_json({"type": "wizard_notice", "text": "❌ No GDD session found — nothing to finish."})
                        # reset local wizard state
                        gdd_wizard_active[session] = False
                        gdd_wizard_stage[session] = 0
                        return

                    print("📡 Calling /gdd/finish for gdd_sid:", gdd_sid)
                    async with httpx.AsyncClient(timeout=20.0) as client:
                        res = await client.post("http://localhost:8000/gdd/finish", json={"session_id": gdd_sid})

                    print("📡 /gdd/finish STATUS:", res.status_code)
                    print("📡 /gdd/finish RESPONSE:", res.text)

                    if res.status_code == 200:
                        data = res.json()
                        await ws.send_json({"type": "wizard_notice", "text": "📘 **Your GDD is ready!**"})
                        await ws.send_json({"type": "final", "text": data.get("markdown", "")})
                    else:
                        await ws.send_json({"type": "wizard_notice", "text": f"❌ Error generating GDD (status {res.status_code})."})
                except Exception as e:
                    print("❌ ERROR inside _finish():", e)
                    try:
                        await ws.send_json({"type": "wizard_notice", "text": f"❌ Exception generating GDD: {e}"})
                    except: pass
                finally:
                    # clear mapping and local wizard state
                    gdd_session_map.pop(session, None)
                    gdd_wizard_active[session] = False
                    gdd_wizard_stage[session] = 0

            asyncio.run_coroutine_threadsafe(_finish(), loop)
            return



        # ⭐⭐⭐ 4) Wizard Answer — AFTER Finish GDD ⭐⭐⭐
        # ---------------------------------------------------------
        # Wizard ANSWER (VOICE) - Only treat as answer if NOT command
        # ---------------------------------------------------------
        # ---------------------------------------------------------
        if gdd_wizard_active.get(session, False):

            # List of phrases that are NOT actual answers
            cmd_phrases = [
                "go next", "next question", "next",
                "finish gdd", "finish", "complete gdd",
                "activate gdd", "activate wizard",
                "call next"
            ]

            # If message contains ANY command → DO NOT treat as answer
            if any(phrase in normalized for phrase in cmd_phrases):
                print("🛑 SKIP ANSWER (voice command detected):", raw_text)
                # Let GO-NEXT or FINISH handlers proceed normally
                # DO NOT return, because "Go next" should be caught by next block
            else:
                # EXTRA FILTER — Reject extremely short/noisy answers
                if len(raw_text.split()) < 3:
                    print("🛑 SKIP short/noisy fragment:", raw_text)
                    return   # <-- IMPORTANT: STOP HERE, do NOT save

                # ACCEPT AS REAL ANSWER
                async def _record():
                    import httpx
                    try:
                        gdd_sid = gdd_session_map.get(session)
                        if not gdd_sid:
                            print("❌ No GDD SID for answer")
                            return

                        print("💾 Saving REAL answer:", raw_text)
                        async with httpx.AsyncClient(timeout=10) as client:
                            await client.post(
                                "http://localhost:8000/gdd/answer",
                                json={"session_id": gdd_sid, "answer": raw_text}
                            )
                        await ws.send_json({"type": "wizard_answer", "text": raw_text})

                    except Exception as e:
                        print("❌ /gdd/answer failed:", e)

                asyncio.run_coroutine_threadsafe(_record(), loop)
                return  # <-- Prevent fallthrough


        # ---------------------------------------------------------
        # Normal LLM flow (unchanged)
        # ---------------------------------------------------------
        async def handle_final():
            await ws.send_json({"type": "final", "text": raw_text})
            llm_stop_flags[session] = False
            sentence_buffer[session] = ""

            try:
                async for token in stream_llm(raw_text):
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
                        if session not in tts_playback_task or tts_playback_task[session].done():
                            tts_playback_task[session] = asyncio.create_task(tts_playback_worker(session))

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
                    tts_playback_task[session] = asyncio.create_task(tts_playback_worker(session))

            await ws.send_json({"type": "llm_done"})

        asyncio.run_coroutine_threadsafe(handle_final(), loop)


    recognizer.recognizing.connect(recognizing)
    recognizer.recognized.connect(recognized)
    recognizer.start_continuous_recognition_async().get()

    print("🎤 Azure STT started successfully")

    # -------------------------------
    # MAIN WS LOOP
    # -------------------------------
    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.disconnect":
                break

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
                        llm_stop_flags[session] = True
                        if session in tts_cancel_events:
                            tts_cancel_events[session].set()

                        cancel_tts_generation(session)
                        worker = tts_playback_task.get(session)
                        if worker and not worker.done():
                            try: worker.cancel()
                            except: pass

                        tts_cancel_events[session] = asyncio.Event()
                        assistant_is_speaking[session] = False
                        await ws.send_json({"type": "stop_all"})
                        continue

            if msg.get("bytes"):
                try: push_stream.write(msg["bytes"])
                except: pass

    finally:
        try: push_stream.close()
        except: pass
        try: recognizer.stop_continuous_recognition()
        except: pass

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

