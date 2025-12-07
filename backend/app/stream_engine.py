# =====================================================================
#  stream_engine.py — patched to handle both coroutine and async-generator
#  - robust TTS streaming (handles async generator or coroutine->bytes)
#  - export/download voice triggers added
# =====================================================================

import uuid
import json
import asyncio
import httpx
import azure.cognitiveservices.speech as speechsdk

from fastapi import WebSocket

from .config import CONFIG
from .llm_orchestrator import stream_llm
from .session_state import (
    llm_stop_flags,
    gdd_wizard_active,
    gdd_session_map,
    ensure_structs,
    cancel_tts_generation,
)
from .tts_engine import async_tts   # may be coroutine returning bytes OR async-generator

AZURE_SPEECH_KEY = CONFIG["AZURE_SPEECH_KEY"]
AZURE_SPEECH_REGION = CONFIG["AZURE_SPEECH_REGION"]


# ================================================================
# HTTP HELPERS
# ================================================================
async def http_post(url: str, payload: dict):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=payload)
        try:
            return r.json()
        except Exception:
            return {"status": "error", "http_status": r.status_code}


async def backend_start(): return await http_post("http://localhost:8000/gdd/start", {})
async def backend_answer(session, text):
    sid = gdd_session_map.get(session)
    return await http_post("http://localhost:8000/gdd/answer", {"session_id": sid, "answer": text}) if sid else None
async def backend_next(session):
    sid = gdd_session_map.get(session)
    return await http_post("http://localhost:8000/gdd/next", {"session_id": sid}) if sid else None
async def backend_finish(session):
    sid = gdd_session_map.get(session)
    return await http_post("http://localhost:8000/gdd/finish", {"session_id": sid}) if sid else None


# ================================================================
# STREAM TTS AUDIO TO CLIENT (REAL PCM) — robust version
# ================================================================
async def stream_tts_to_ws(ws: WebSocket, text: str):
    """
    Convert text -> PCM audio using async_tts and stream to ws.
    Handles two async_tts styles:
      - async generator yielding bytes chunks (async for ...)
      - coroutine returning bytes (await result)
    Sends binary frames (ws.send_bytes) and finalizes with voice_done.
    """
    try:
        maybe = async_tts(text)
        # If it's an async iterable, iterate
        if hasattr(maybe, "__aiter__"):
            async for chunk in maybe:
                try:
                    if isinstance(chunk, (bytes, bytearray)):
                        await ws.send_bytes(chunk)
                    else:
                        await ws.send_bytes(bytes(chunk))
                except Exception as e:
                    print("stream_tts_to_ws: failed sending chunk:", e)
                    break
        else:
            # It's a coroutine returning full bytes (common in your current tts_engine.py)
            try:
                audio_bytes = await maybe
                if audio_bytes:
                    await ws.send_bytes(audio_bytes)
            except Exception as e:
                print("stream_tts_to_ws: coroutine tts failed:", e)

    except Exception as e:
        print("TTS stream error:", e)
    finally:
        # Let frontend know TTS finished for this spoken segment
        try:
            await ws.send_json({"type": "voice_done"})
        except:
            pass


# ================================================================
# SEND QUESTION TO UI + TTS
# ================================================================
async def send_question(ws: WebSocket, q: dict):
    if not q:
        return

    # Wizard completed
    if q.get("status") == "done":
        await ws.send_json({"type": "gdd_done"})
        asyncio.create_task(stream_tts_to_ws(ws,
            "All questions are completed. Say Finish G D D to generate the document."
        ))
        return

    question = q.get("question") or ""
    idx = q.get("index", 0)
    total = q.get("total", "?")

    # UI bubble
    await ws.send_json({
        "type": "gdd_next",
        "question": question,
        "index": idx,
        "total": total
    })

    # TTS voiceover (non-blocking)
    asyncio.create_task(stream_tts_to_ws(ws, question))


# ================================================================
# Export trigger helper
# ================================================================
async def trigger_export_ui(ws: WebSocket, session: str):
    """
    Tell client to start export flow. Client can call /gdd/export with session_id.
    """
    sid = gdd_session_map.get(session)
    if not sid:
        await ws.send_json({"type": "gdd_export_ready", "error": "no_session"})
        return
    await ws.send_json({"type": "gdd_export_ready", "session_id": sid})


# ================================================================
# HANDLE TYPED INPUT
# ================================================================
EXPORT_TRIGGERS = [
    "export gdd", "download gdd", "export document", "export doc",
    "download document", "download gdd doc", "export"
]

async def handle_text_message(ws: WebSocket, text: str, session: str):
    raw = text.strip()
    lower = raw.lower()

    # -------------------------------------------------------------
    # EXPORT COMMANDS (typed) — handle first
    # -------------------------------------------------------------
    if any(t in lower for t in EXPORT_TRIGGERS):
        await trigger_export_ui(ws, session)
        return

    # -------------------------------------------------------------
    # START WIZARD (typed)
    # -------------------------------------------------------------
    if "activate gdd" in lower or "gdd wizard" in lower or "activate g d d" in lower:
        gdd_wizard_active[session] = True

        start = await backend_start()
        sid = start.get("session_id")
        if sid:
            gdd_session_map[session] = sid
            await ws.send_json({"type": "gdd_session_id", "session_id": sid})
            await ws.send_json({"type": "wizard_answer", "text": raw})
            q = await backend_next(session)
            await send_question(ws, q)
        else:
            await ws.send_json({"type": "wizard_error", "msg": "failed to start"})
        return

    # -------------------------------------------------------------
    # FINISH GDD (typed)
    # -------------------------------------------------------------
    if lower.startswith("finish gdd") or lower.startswith("generate gdd"):
        finish = await backend_finish(session)
        if finish and finish.get("status") == "ok":
            await ws.send_json({
                "type": "gdd_complete",
                "markdown": finish.get("markdown")
            })
            asyncio.create_task(stream_tts_to_ws(ws, "Your Game Design Document is ready."))
        else:
            await ws.send_json({"type": "gdd_error", "msg": "finish_failed"})
        return

    # -------------------------------------------------------------
    # WIZARD ANSWER
    # -------------------------------------------------------------
    if gdd_wizard_active.get(session):
        if "go next" in lower or lower.strip() == "next":
            q = await backend_next(session)
            await send_question(ws, q)
            return

        # Normal wizard answer
        await ws.send_json({"type": "wizard_answer", "text": raw})
        await backend_answer(session, raw)
        return

    # -------------------------------------------------------------
    # NORMAL CHAT MODE
    # -------------------------------------------------------------
    await ws.send_json({"type": "final", "text": raw})

    llm_stop_flags[session] = False
    async for token in stream_llm(raw):
        if llm_stop_flags.get(session):
            break
        await ws.send_json({"type": "llm_stream", "token": token})

    await ws.send_json({"type": "llm_done"})


# ================================================================
# VOICE STREAM HANDLING
# ================================================================
async def azure_stream(ws: WebSocket):
    session = str(uuid.uuid4())
    print("WS connected:", session)

    ensure_structs(session)
    llm_stop_flags[session] = False

    push_stream = speechsdk.audio.PushAudioInputStream(
        stream_format=speechsdk.audio.AudioStreamFormat(16000, 16, 1)
    )

    recognizer = speechsdk.SpeechRecognizer(
        speech_config=speechsdk.SpeechConfig(
            subscription=AZURE_SPEECH_KEY,
            region=AZURE_SPEECH_REGION
        ),
        audio_config=speechsdk.audio.AudioConfig(stream=push_stream)
    )

    loop = asyncio.get_event_loop()

    # ---------------------------------------------------------
    # FINAL STT HANDLER
    # ---------------------------------------------------------
    def on_stt(evt):
        if evt.result.reason != speechsdk.ResultReason.RecognizedSpeech:
            return

        raw = evt.result.text.strip()
        if not raw:
            return

        print("🟢 Final STT:", raw)
        lower = raw.lower()

        # ----------------- EXPORT (voice) -----------------
        if any(t in lower for t in EXPORT_TRIGGERS):
            async def _export():
                await trigger_export_ui(ws, session)
            asyncio.run_coroutine_threadsafe(_export(), loop)
            return

        # ----------------- START WIZARD (voice) -----------------
        if "activate gdd" in lower or "gdd wizard" in lower or "activate g d d" in lower:
            gdd_wizard_active[session] = True

            async def _start():
                start = await backend_start()
                sid = start.get("session_id")
                if sid:
                    gdd_session_map[session] = sid
                    await ws.send_json({"type": "gdd_session_id", "session_id": sid})
                    await ws.send_json({"type": "wizard_answer", "text": raw})
                    q = await backend_next(session)
                    await send_question(ws, q)
                else:
                    await ws.send_json({"type": "wizard_error", "msg": "start_failed"})
            asyncio.run_coroutine_threadsafe(_start(), loop)
            return

        # ----------------- FINISH GDD (voice) -----------------
        if lower.startswith("finish gdd") or "finish the gdd" in lower:
            async def _finish():
                finish = await backend_finish(session)
                if finish and finish.get("status") == "ok":
                    await ws.send_json({
                        "type": "gdd_complete",
                        "markdown": finish.get("markdown")
                    })
                    asyncio.create_task(stream_tts_to_ws(ws, "Your Game Design Document is ready."))
                else:
                    await ws.send_json({"type": "gdd_error", "msg": "finish_failed"})
            asyncio.run_coroutine_threadsafe(_finish(), loop)
            return

        # ----------------- WIZARD ANSWERING (voice) -----------------
        if gdd_wizard_active.get(session):
            if "go next" in lower or lower == "next":
                async def _next():
                    q = await backend_next(session)
                    await send_question(ws, q)
                asyncio.run_coroutine_threadsafe(_next(), loop)
                return

            async def _answer():
                await ws.send_json({"type": "wizard_answer", "text": raw})
                await backend_answer(session, raw)
            asyncio.run_coroutine_threadsafe(_answer(), loop)
            return

        # ----------------- NORMAL CHAT (voice) -----------------
        async def _chat():
            await ws.send_json({"type": "final", "text": raw})
            llm_stop_flags[session] = False

            async for token in stream_llm(raw):
                if llm_stop_flags.get(session):
                    break
                await ws.send_json({"type": "llm_stream", "token": token})

            await ws.send_json({"type": "llm_done"})

        asyncio.run_coroutine_threadsafe(_chat(), loop)


    recognizer.recognized.connect(on_stt)
    recognizer.start_continuous_recognition_async().get()
    print("🎤 Azure STT started successfully")

    # ================================================================
    # MAIN LOOP
    # ================================================================
    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.disconnect":
                break

            # TEXT MESSAGE
            if msg.get("text"):
                try:
                    data = json.loads(msg["text"])
                except:
                    data = {}

                if data.get("type") == "text":
                    asyncio.create_task(handle_text_message(ws, data["text"], session))
                    continue

                if data.get("type") == "stop_llm":
                    llm_stop_flags[session] = True
                    cancel_tts_generation(session)
                    await ws.send_json({"type": "stop_all"})
                    continue

            # AUDIO BYTES
            if msg.get("bytes"):
                try:
                    push_stream.write(msg["bytes"])
                except:
                    pass

    finally:
        try:
            push_stream.close()
        except:
            pass
        try:
            recognizer.stop_continuous_recognition()
        except:
            pass

        cancel_tts_generation(session)
        print("WS closed:", session)
