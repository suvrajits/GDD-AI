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
# Smart Completion buffer
pending_user_text = {}      # session → last STT final text
completion_timer = {}       # session → asyncio.Task
pending_review_task = {}
gdd_answer_buffer = {} 
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

        # 🔥 REQUIRED NEW INITIALIZATIONS
    pending_review_task.setdefault(session, None)
    gdd_answer_buffer.setdefault(session, [])

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


# --------------------------------------------------------------------
# ONE-SHOT LLM REVIEW FUNCTION (Azure Realtime, same as stream_llm)
# --------------------------------------------------------------------
async def run_llm_short_review(prompt: str) -> str:
    """
    Runs a short Azure review using the SAME Realtime pipeline
    used by stream_llm().
    """
    system = (
        "You are a collaborative, visionary game director helping a designer shape ideas.\n"
        "Your tone is creative, suggestive, additive — never strict or corrective.\n"
        "Build on the user's idea with possibilities, expansions, and thoughtful suggestions.\n"
        "Avoid policing language such as 'missing', 'incorrect', or 'does not address'.\n"
        "Always assume the user is exploring, not failing — respond as a co-creator.\n"
        "Keep feedback concise (2–4 sentences) but inspiring.\n"
        "Offer optional directions the idea could evolve into, and invite refinement."
    )



    full_prompt = f"{system}\n\nUser Answer:\n{prompt}\n\nYour response:"
    full_text = ""

    async for token in stream_llm(full_prompt):
        if token:
            full_text += token

    return full_text.strip()

# ------------------------------------------------------------------
# SMART COMPLETION 2.0 — Incomplete Answer Detection + Nudges
# ------------------------------------------------------------------

INCOMPLETE_MARKERS = (
    "uh", "um", "so", "because", "like", "kinda", "sort of",
    "basically", "i mean", "you know", "and", "which", "but"
)

NUDGES = [
    "Go on…",
    "Would you like to elaborate?",
    "Feel free to continue.",
    "Take your time — you can expand.",
    "If that’s your full answer, I can respond — just let me know.",
]

def is_incomplete_answer(text: str) -> bool:
    text = text.strip().lower()

    # Too short to evaluate (1–2 words) = always incomplete
    if len(text.split()) <= 2:
        return True

    # Hesitation markers at END
    if any(text.endswith(m) for m in INCOMPLETE_MARKERS):
        return True

    # Trailing ellipsis
    if text.endswith("..."):
        return True

    # Mid-sentence hesitation
    if text.endswith(("uh", "um", "hmm")):
        return True

    # Ends with conjunction/comma = user is continuing
    if re.search(r"(and|but|which|so|because|like|kinda|sort of)[\s]*$", text):
        return True

    # Short but *complete* answers (3–6 words) should be accepted
    if 3 <= len(text.split()) <= 6:
        return False

    # Default: answer appears complete
    return False


def pick_nudge() -> str:
    import random
    return random.choice(NUDGES)


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

        # INTERRUPT ANY ACTIVE LLM/TTS
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
            await ws.send_json({"type": "stop_all"})
        except:
            pass

        tts_cancel_events[session] = asyncio.Event()
        assistant_is_speaking[session] = False

        try:
            from app.gdd_engine.gdd_questions import QUESTIONS
        except Exception:
            try:
                from gdd_engine.gdd_questions import QUESTIONS
            except Exception:
                QUESTIONS = []

        gdd_wizard_active[session] = True
        gdd_wizard_stage[session] = 0
        gdd_answer_buffer[session] = []  # 
        task = pending_review_task.get(session)
        if task and not task.done():
            try: task.cancel()
            except: pass
        pending_review_task[session] = None


        async def _start():
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    res = await client.post("http://localhost:8000/gdd/start")

                if res.status_code == 200:
                    j = res.json()
                    gdd_session_map[session] = j.get("session_id", "")
                    try:
                        await ws.send_json({"type": "gdd_session_id", "session_id": gdd_session_map[session]})
                    except:
                        pass
                else:
                    print("⚠ /gdd/start failed:", res.status_code)

            except Exception as e:
                print("❌ Exception calling /gdd/start:", e)

        asyncio.create_task(_start())

        try:
            await ws.send_json({"type": "final", "text": raw_text})
        except:
            pass

        try:
            await ws.send_json({
                "type": "wizard_notice",
                "text": "🎮 **GDD Wizard Activated!** Say *Go Next* anytime.",
                "wizard_active": True
            })

            if QUESTIONS:
                await ws.send_json({"type": "llm_done"})
                await ws.send_json({
                    "type": "wizard_question",
                    "text": QUESTIONS[0],
                    "index": 0,
                    "total": len(QUESTIONS),
                    "voice": QUESTIONS[0]
                })

                cleaned = clean_sentence_for_tts(QUESTIONS[0])
                if cleaned:
                    enqueue_sentence_for_tts(session, cleaned, source="wizard")

        except Exception:
            pass

        return True

    # -------- GO NEXT ----------
    # -------- GO NEXT ----------
    if gdd_wizard_active.get(session, False) and ("go next" in normalized or normalized == "next"):

        try:
            from app.gdd_engine.gdd_questions import QUESTIONS
        except:
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

        # 🔥 Cancel any pending delayed-review from the previous question
        task = pending_review_task.get(session)
        if task and not task.done():
            try:
                task.cancel()
                print(f"[{session}] GO NEXT -> cancelled pending review task")
            except:
                pass
        pending_review_task[session] = None
        gdd_wizard_stage[session] = stage
        gdd_answer_buffer[session] = []


        try:
            await ws.send_json({"type": "llm_done"})
            await ws.send_json({
                "type": "wizard_question",
                "text": QUESTIONS[stage],
                "index": stage,
                "total": len(QUESTIONS),
                "voice": QUESTIONS[stage]
            })
        except:
            pass

        cleaned = clean_sentence_for_tts(QUESTIONS[stage])
        if cleaned:
            enqueue_sentence_for_tts(session, cleaned, source="wizard")

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
                    await ws.send_json({"type": "wizard_notice", "text": f"❌ Error generating GDD ({res.status_code})."})

            except Exception as e:
                print("❌ ERROR inside _finish():", e)
                await ws.send_json({"type": "wizard_notice", "text": "❌ Exception generating GDD."})

            finally:
                gdd_wizard_active[session] = False

        asyncio.create_task(_finish())
        return True

    # -------- EXPORT ----------
    if any(p in normalized for p in ("export gdd", "export the gdd", "download gdd", "export document")):

        gdd_sid = gdd_session_map.get(session)
        if not gdd_sid:
            await ws.send_json({"type": "wizard_notice", "text": "❌ No GDD available to export. Finish GDD first."})
            return True

        async def _export():
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.post("http://localhost:8000/gdd/export", json={"session_id": gdd_sid})

                if res.status_code != 200:
                    await ws.send_json({"type": "wizard_notice", "text": f"❌ Export failed ({res.status_code})."})
                    return

                await ws.send_json({"type": "gdd_export_ready", "filename": f"GDD_{gdd_sid}.docx"})

            except Exception:
                await ws.send_json({"type": "wizard_notice", "text": "❌ Export failed."})

        asyncio.create_task(_export())
        return True

    # ------------------------------------------------------------------
    # -------- SAVE ANSWER (THE BLOCK YOU NEEDED FIXED) ---------------
    # ------------------------------------------------------------------
    if gdd_wizard_active.get(session, False):

        noise = {".", "uh", "um", ""}
        if raw_text.lower().strip() in noise:
            return True

        async def _record_answer():
            try:
                task = pending_review_task.get(session)
                if task and not task.done():
                    try:
                        task.cancel()
                    except:
                        pass

                await ws.send_json({"type": "wizard_answer", "text": raw_text})

                # 🔥 Add this:
                gdd_answer_buffer.setdefault(session, []).append(raw_text.strip())


                # =============== DEFINE _review() ====================
                async def _review():
                    try:
                        from app.gdd_engine.gdd_questions import QUESTIONS
                        stage = gdd_wizard_stage.get(session, 0)
                        question_text = QUESTIONS[stage] if 0 <= stage < len(QUESTIONS) else ""
                        answer = " ".join(gdd_answer_buffer[session]).strip()


                        # 1) Incomplete → nudge only
                        last = gdd_answer_buffer[session][-1]
                        if is_incomplete_answer(answer):
                            nudge = pick_nudge()
                            await ws.send_json({"type": "ai_review", "text": nudge})

                            if not assistant_is_speaking.get(session, False):
                                cleaned = clean_sentence_for_tts(nudge)
                                if cleaned:
                                    enqueue_sentence_for_tts(session, cleaned, source="wizard")
                            return

                        # 2) Short but complete → elaboration
                        # 2) SHORT BUT COMPLETE ANSWER — BUT EXEMPT explicit requests
                        request_keywords = ("suggest", "suggestion", "ideas", "sensations", "expand", "help", "inspire")

                        if (
                            3 <= len(answer.split()) <= 6 
                            and answer.endswith((".", "!", "?"))
                            and not any(k in answer.lower() for k in request_keywords)
                        ):
                            if "rts" in question_text.lower():
                                nudge = "Would you like to expand on what makes your RTS idea unique?"
                            else:
                                nudge = "Would you like to expand on that thought?"

                            await ws.send_json({"type": "ai_review", "text": nudge})

                            if not assistant_is_speaking.get(session, False):
                                cleaned_nudge = clean_sentence_for_tts(nudge)
                                if cleaned_nudge:
                                    enqueue_sentence_for_tts(session, cleaned_nudge, source="wizard")

                            return


                        # 3) Full critique
                        review_prompt = (
                            f"Question:\n{question_text}\n\n"
                            f"User Answer:\n{answer}\n\n"
                            "Offer 2–4 inspiring, collaborative suggestions that expand the idea. "
                            "Avoid criticism. Build on the user’s creative direction."
                        )

                        review = await run_llm_short_review(review_prompt)
                        await ws.send_json({"type": "ai_review", "text": review})

                        cleaned = clean_sentence_for_tts(review)
                        if cleaned:
                            enqueue_sentence_for_tts(session, cleaned, source="wizard")

                    except Exception as e:
                        print("❌ LLM review failed:", e)

                # =============== DELAYED REVIEW ====================
                async def delayed_review():
                    await asyncio.sleep(1.8)

                    # cancel if user resumed speaking
                    if pending_user_text.get(session) not in gdd_answer_buffer.get(session, []):
                        return


                    # Recompute answer for scope correctness
                    answer_local = " ".join(gdd_answer_buffer.get(session, []))


                    try:
                        from app.gdd_engine.gdd_questions import QUESTIONS
                        stage_local = gdd_wizard_stage.get(session, 0)
                        question_text_local = QUESTIONS[stage_local] if 0 <= stage_local < len(QUESTIONS) else ""
                    except:
                        question_text_local = ""

                    # Short but complete → nudge
                    request_keywords = ("suggest", "suggestion", "ideas", "sensations", "expand", "help", "inspire")
                    
                    if (
                        3 <= len(answer_local.split()) <= 6
                        and answer_local.endswith((".", "!", "?"))
                        and not any(k in answer_local.lower() for k in request_keywords)
                    ):

                        if "rts" in question_text_local.lower():
                            nudge = "Would you like to expand on what makes your RTS idea unique?"
                        else:
                            nudge = "Would you like to expand on that thought?"

                        await ws.send_json({"type": "ai_review", "text": nudge})

                        if not assistant_is_speaking.get(session, False):
                            cleaned = clean_sentence_for_tts(nudge)
                            if cleaned:
                                enqueue_sentence_for_tts(session, cleaned, source="wizard")
                        return

                    # Otherwise do full critique
                    await _review()

                pending_review_task[session] = asyncio.create_task(delayed_review())

            except Exception as e:
                print("❌ /gdd/answer failed:", e)

        await _record_answer()
        return True

    return False



async def generate_gdd_answer_review(question: str, answer: str) -> str:
    """
    Generates a short 2–3 sentence LLM review.
    Focuses strictly on the given question.
    """

    review_prompt = f"""
You are a senior game designer reviewing ONE answer to a GDD question.
Stay strictly inside THIS question's context.

QUESTION:
{question}

ANSWER:
{answer}

Provide a short, concise 2–3 sentence suggestion.
Do NOT ask new questions.
Do NOT change topic.
Only critique or refine the answer itself.
"""

    try:
        # Correct import for your orchestrator
        from app.llm_orchestrator import run_completion

        # Send one-shot LLM call
        suggestion = await run_completion(review_prompt, max_tokens=120)

        return suggestion.strip()

    except Exception as e:
        print("❌ LLM review failed:", e)
        return "👍 Answer noted."

def estimate_completion_delay(text: str, is_wizard: bool) -> float:
    """
    Returns natural pause duration.
    Wizard answers need more time.
    Short or incomplete phrases get extra delay.
    """
    text = text.strip().lower()

    # Incomplete thought markers
    incompletes = ("and", "so", "because", "like", "maybe", "i think", "i feel")

    if any(text.endswith(w) for w in incompletes):
        return 1.6

    # If sentence ends properly → quicker confirmation
    if text.endswith((".", "?", "!", "…")):
        return 0.8 if not is_wizard else 1.1

    # Default mid-thought pause
    return 1.2 if not is_wizard else 1.5

async def submit_after_delay(ws, session, delay):
    try:
        await asyncio.sleep(delay)

        text = pending_user_text.get(session, "").strip()
        if not text:
            return

        # Try wizard handling first
        handled = await process_gdd_wizard(ws, session, text)
        if handled:
            return

        # Otherwise handle normal text
        await handle_text_message(ws, text, session)

    except asyncio.CancelledError:
        # User resumed talking — ignore gracefully
        return

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

            # ------------------------------------------------------
            # 1) Forward partial transcript to UI
            # ------------------------------------------------------
            if text:
                try:
                    asyncio.run_coroutine_threadsafe(
                        ws.send_json({"type": "partial", "text": text}),
                        loop
                    )
                except Exception:
                    pass

            # ------------------------------------------------------
            # 2) CANCEL ANY PENDING WIZARD REVIEW — but ONLY if this
            #    partial indicates NEW SPEECH (not duplicate STT)
            # ------------------------------------------------------
            task = pending_review_task.get(session)
            if (
                task
                and not task.done()
                and pending_user_text.get(session) != text   # 🟩 FIXED HERE
            ):
                try:
                    task.cancel()
                    print(f"[{session}] Partial STT -> canceled pending wizard review")
                except Exception:
                    pass
            pending_review_task[session] = None   # ← REQUIRED RESET

            # ------------------------------------------------------
            # 3) INTERRUPT SPEAKING ASSISTANT (barge-in)
            # ------------------------------------------------------
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
                    asyncio.run_coroutine_threadsafe(
                        ws.send_json({"type": "stop_all"}),
                        loop
                    )
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
            if not raw_text or raw_text.lower() in [".", "uh", "um"]:
                return

            print("🟢 Final STT:", raw_text)

            # -------------------------------------------------------
            # 1) IGNORE DUPLICATE FINALS FROM AZURE (CRITICAL)
            # -------------------------------------------------------
            # Azure often emits the same final result multiple times.
            if pending_user_text.get(session) == raw_text:
                print(f"[{session}] Duplicate final STT ignored.")
                return

            # Save latest transcript
            pending_user_text[session] = raw_text

            # -------------------------------------------------------
            # 2) CANCEL ANY PENDING WIZARD REVIEW
            # -------------------------------------------------------
            task = pending_review_task.get(session)
            if task and not task.done():
                try:
                    task.cancel()
                    print(f"[{session}] Final STT -> cancelled pending wizard review task")
                except Exception:
                    pass

            # -------------------------------------------------------
            # 3) CANCEL EXISTING SMART COMPLETION TIMER
            # -------------------------------------------------------
            existing = completion_timer.get(session)
            if existing and not existing.done():
                try:
                    existing.cancel()
                    print(f"[{session}] Final STT -> cancelled old completion timer")
                except Exception:
                    pass

            # -------------------------------------------------------
            # 4) DETERMINE NATURAL DELAY BEFORE PROCESSING TEXT
            # -------------------------------------------------------
            delay = estimate_completion_delay(
                raw_text,
                gdd_wizard_active.get(session, False)
            )

            # -------------------------------------------------------
            # 5) START DELAYED SUBMISSION (SMART COMPLETION 2.0)
            # -------------------------------------------------------
            completion_timer[session] = asyncio.run_coroutine_threadsafe(
                submit_after_delay(ws, session, delay),
                loop
            )

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
