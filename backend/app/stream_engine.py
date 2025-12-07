# stream_engine.py v4
# Fully patched: wizard TTS + LLM reply TTS (uses same TTS voice as questions)
# - Handles async-generator or coroutine async_tts
# - Accumulates LLM tokens and speaks final reply
# - Supports export/download voice commands
# - Non-blocking TTS (asyncio.create_task)
# - Emits voice_done at end of each TTS segment

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
from .tts_engine import async_tts   # uses the same voice pipeline you already use

AZURE_SPEECH_KEY = CONFIG.get("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION = CONFIG.get("AZURE_SPEECH_REGION")


# ------------------------------------------------------------------
# HTTP helpers to backend gdd endpoints
# ------------------------------------------------------------------
async def http_post(url: str, payload: dict):
    async with httpx.AsyncClient(timeout=20) as client:
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


# ------------------------------------------------------------------
# Robust TTS streaming helper
# - Accepts async generator OR coroutine -> bytes
# - Sends binary frames (PCM) to the websocket
# - Sends {"type":"voice_done"} JSON when finished
# ------------------------------------------------------------------
async def stream_tts_to_ws(ws: WebSocket, text: str):
    """
    Convert `text` -> PCM audio via async_tts and stream to ws as binary frames.
    Works whether async_tts returns an async-generator (yielding chunks)
    or a coroutine that returns bytes.
    """
    if not text:
        try:
            await ws.send_json({"type": "voice_done"})
        except:
            pass
        return

    try:
        maybe = async_tts(text)

        if hasattr(maybe, "__aiter__"):
            # async generator
            async for chunk in maybe:
                try:
                    if isinstance(chunk, (bytes, bytearray)):
                        await ws.send_bytes(chunk)
                    else:
                        # fallback: convert to bytes
                        await ws.send_bytes(bytes(chunk))
                except Exception as e:
                    print("stream_tts_to_ws: send chunk error:", e)
                    break
        else:
            # coroutine returning full bytes (common)
            try:
                audio_bytes = await maybe
                if audio_bytes:
                    await ws.send_bytes(audio_bytes)
            except Exception as e:
                print("stream_tts_to_ws: awaited tts coroutine error:", e)

    except Exception as e:
        print("TTS stream error:", e)
    finally:
        # Always send a voice_done event so frontend finalizes the streaming bubble
        try:
            await ws.send_json({"type": "voice_done"})
        except Exception:
            pass


# ------------------------------------------------------------------
# Send question to UI and trigger TTS voiceover (non-blocking)
# ------------------------------------------------------------------
async def send_question(ws: WebSocket, q: dict):
    if not q:
        return

    if q.get("status") == "done":
        await ws.send_json({"type": "gdd_done"})
        asyncio.create_task(stream_tts_to_ws(ws, "All questions are completed. Say Finish G D D to generate the document."))
        return

    question = q.get("question", "")
    idx = q.get("index", 0)
    total = q.get("total", "?")

    await ws.send_json({
        "type": "gdd_next",
        "question": question,
        "index": idx,
        "total": total
    })

    # speak the question (non-blocking)
    asyncio.create_task(stream_tts_to_ws(ws, question))


# ------------------------------------------------------------------
# Export helper: notify client that export is ready (send session id)
# ------------------------------------------------------------------
async def trigger_export_ui(ws: WebSocket, session: str):
    sid = gdd_session_map.get(session)
    if not sid:
        await ws.send_json({"type": "gdd_export_ready", "error": "no_session"})
        return
    await ws.send_json({"type": "gdd_export_ready", "session_id": sid})


EXPORT_TRIGGERS = [
    "export gdd", "download gdd", "export document", "export doc",
    "download document", "download gdd doc", "export"
]


# ------------------------------------------------------------------
# Handle typed text messages (entry from frontend)
# ------------------------------------------------------------------
async def handle_text_message(ws: WebSocket, text: str, session: str):
    raw = (text or "").strip()
    lower = raw.lower()

    # 1) Export commands
    if any(t in lower for t in EXPORT_TRIGGERS):
        await trigger_export_ui(ws, session)
        return

    # 2) Start wizard (typed)
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
            await ws.send_json({"type": "wizard_error", "msg": "start_failed"})
        return

    # 3) Finish GDD (typed)
    if lower.startswith("finish gdd") or lower.startswith("generate gdd"):
        finish = await backend_finish(session)
        if finish and finish.get("status") == "ok":
            await ws.send_json({"type": "gdd_complete", "markdown": finish.get("markdown")})
            asyncio.create_task(stream_tts_to_ws(ws, "Your Game Design Document is ready."))
        else:
            await ws.send_json({"type": "gdd_error", "msg": "finish_failed"})
        return

    # 4) Wizard flow
    if gdd_wizard_active.get(session):
        if "go next" in lower or lower.strip() == "next":
            q = await backend_next(session)
            await send_question(ws, q)
            return

        # ordinar answer to current question
        await ws.send_json({"type": "wizard_answer", "text": raw})
        await backend_answer(session, raw)
        return

    # 5) Normal chat: stream LLM tokens to UI; also TTS final reply
    await ws.send_json({"type": "final", "text": raw})

    llm_stop_flags[session] = False
    collected = []

    try:
        async for token in stream_llm(raw):
            if llm_stop_flags.get(session):
                break
            collected.append(token)
            await ws.send_json({"type": "llm_stream", "token": token})
    except Exception as e:
        print("stream_llm error:", e)

    await ws.send_json({"type": "llm_done"})

    final_reply = "".join(collected).strip()
    if final_reply:
        # speak final reply using same TTS voice as questions
        asyncio.create_task(stream_tts_to_ws(ws, final_reply))


# ------------------------------------------------------------------
# Voice stream: Azure STT integration + main loop
# ------------------------------------------------------------------
async def azure_stream(ws: WebSocket):
    session = str(uuid.uuid4())
    print("WS connected:", session)

    ensure_structs(session)
    llm_stop_flags[session] = False

    # Push stream for Azure recognizer (expecting 16kHz 16bit mono PCM)
    push_stream = speechsdk.audio.PushAudioInputStream(
        stream_format=speechsdk.audio.AudioStreamFormat(16000, 16, 1)
    )

    speech_config = speechsdk.SpeechConfig(subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION)
    recognizer = speechsdk.SpeechRecognizer(
        speech_config=speech_config,
        audio_config=speechsdk.audio.AudioConfig(stream=push_stream)
    )

    loop = asyncio.get_event_loop()

    # Final STT (recognized) handler
    def on_stt(evt):
        if evt.result.reason != speechsdk.ResultReason.RecognizedSpeech:
            return

        raw = evt.result.text.strip()
        if not raw:
            return

        print("🟢 Final STT:", raw)
        lower = raw.lower()

        # Export (voice)
        if any(t in lower for t in EXPORT_TRIGGERS):
            async def _export():
                await trigger_export_ui(ws, session)
            asyncio.run_coroutine_threadsafe(_export(), loop)
            return

        # Start wizard (voice)
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

        # Finish GDD (voice)
        if lower.startswith("finish gdd") or "finish the gdd" in lower:
            async def _finish():
                finish = await backend_finish(session)
                if finish and finish.get("status") == "ok":
                    await ws.send_json({"type": "gdd_complete", "markdown": finish.get("markdown")})
                    asyncio.create_task(stream_tts_to_ws(ws, "Your Game Design Document is ready."))
                else:
                    await ws.send_json({"type": "gdd_error", "msg": "finish_failed"})
            asyncio.run_coroutine_threadsafe(_finish(), loop)
            return

        # Wizard answering (voice)
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

        # Normal chat (voice) — stream tokens, accumulate, then TTS final reply
        async def _chat():
            await ws.send_json({"type": "final", "text": raw})
            llm_stop_flags[session] = False
            collected = []
            try:
                async for token in stream_llm(raw):
                    if llm_stop_flags.get(session):
                        break
                    collected.append(token)
                    await ws.send_json({"type": "llm_stream", "token": token})
            except Exception as e:
                print("stream_llm error (voice):", e)

            await ws.send_json({"type": "llm_done"})
            final_reply = "".join(collected).strip()
            if final_reply:
                asyncio.create_task(stream_tts_to_ws(ws, final_reply))

        asyncio.run_coroutine_threadsafe(_chat(), loop)

    recognizer.recognized.connect(on_stt)
    recognizer.start_continuous_recognition_async().get()
    print("🎤 Azure STT started successfully")

    # Main loop: receive websocket frames (text or binary)
    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.disconnect":
                break

            # TEXT frames from client
            if msg.get("text"):
                try:
                    data = json.loads(msg["text"])
                except Exception:
                    data = {}

                if data.get("type") == "text":
                    asyncio.create_task(handle_text_message(ws, data.get("text", ""), session))
                    continue

                if data.get("type") == "stop_llm":
                    llm_stop_flags[session] = True
                    cancel_tts_generation(session)
                    await ws.send_json({"type": "stop_all"})
                    continue

            # BINARY audio frames (client mic)
            if msg.get("bytes"):
                try:
                    push_stream.write(msg["bytes"])
                except Exception:
                    pass

    finally:
        try:
            push_stream.close()
        except Exception:
            pass
        try:
            recognizer.stop_continuous_recognition()
        except Exception:
            pass

        cancel_tts_generation(session)
        print("WS closed:", session)
