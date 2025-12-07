# app/stream_engine.py
import uuid
import json
import re
import asyncio
import httpx
import azure.cognitiveservices.speech as speechsdk

from fastapi import WebSocket

from .config import CONFIG
from .llm_orchestrator import stream_llm

from .session_state import (
    llm_stop_flags, user_last_input_was_voice, sentence_buffer,
    tts_sentence_queue, tts_gen_tasks, tts_cancel_events, tts_playback_task,
    playback_ws_registry, assistant_is_speaking,
    gdd_wizard_active, gdd_wizard_stage, gdd_session_map,
    ensure_structs, cancel_tts_generation
)

from .tts_engine import async_tts, tts_playback_worker, clean_sentence_for_tts

AZURE_SPEECH_KEY = CONFIG["AZURE_SPEECH_KEY"]
AZURE_SPEECH_REGION = CONFIG["AZURE_SPEECH_REGION"]

# helper to load QUESTIONS safely (original code attempted two import locations)
def load_questions():
    try:
        from app.gdd_engine.gdd_questions import QUESTIONS
    except Exception:
        try:
            from gdd_engine.gdd_questions import QUESTIONS
        except:
            QUESTIONS = []
    return QUESTIONS

async def handle_text_message(ws: WebSocket, text: str, session: str):
    """
    Text-path handler: mirrors original main.py handle_text_message.
    Supports wizard activation, 'go next', wizard answers, and normal LLM mode.
    """
    text = text.strip()
    if not text:
        return

    lower = text.lower().strip()
    normalized = lower.replace(".", "").replace("?", "").replace("!", "")
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.replace("g d d", "gdd").replace("g d", "gd")

    activation_phrases = [
        "activate gdd wizard", "activate gd wizard",
        "activate the gdd wizard", "start gdd wizard",
        "start the gdd wizard", "open gdd wizard",
        "launch gdd wizard", "activate wizard", "start wizard",
    ]

    if any(p in normalized for p in activation_phrases):
        QUESTIONS = load_questions()

        gdd_wizard_active[session] = True
        gdd_wizard_stage[session] = 0

        # create backend GDD session (best-effort)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.post("http://localhost:8000/gdd/start")
            j = res.json()
            real_sid = j.get("session_id")
            gdd_session_map[session] = real_sid

            # send real session id to frontend
            await ws.send_json({"type": "gdd_session_id", "session_id": real_sid})
        except Exception as e:
            print("❌ Could not create backend gdd session (text path):", e)

        await ws.send_json({
            "type": "wizard_notice",
            "text": "🎮 **GDD Wizard Activated!** Say *Go Next* anytime."
        })

        if QUESTIONS:
            await ws.send_json({
                "type": "wizard_question",
                "text": f"{QUESTIONS[0]}",
                "voice": f"{QUESTIONS[0]}"
            })

        # queue TTS for this question
        async def _enqueue_first_question():
            cleaned = clean_sentence_for_tts(QUESTIONS[0])
            if cleaned:
                tts_sentence_queue[session].append(cleaned)
                task = asyncio.create_task(async_tts(cleaned))
                tts_gen_tasks[session].append(task)
                if session not in tts_playback_task or tts_playback_task[session].done():
                    tts_playback_task[session] = asyncio.create_task(tts_playback_worker(session))

        asyncio.run_coroutine_threadsafe(_enqueue_first_question(), asyncio.get_event_loop())
        return

    # If in wizard mode and 'go next'
    if gdd_wizard_active.get(session, False) and "go next" in normalized:
        try:
            real_sid = gdd_session_map.get(session)
            if not real_sid:
                # create backend session if missing
                async with httpx.AsyncClient(timeout=5.0) as client:
                    res = await client.post("http://localhost:8000/gdd/start")
                    if res.status_code == 200:
                        j = res.json()
                        real_sid = j.get("session_id")
                        gdd_session_map[session] = real_sid
        except Exception as e:
            print("❌ Could not create backend gdd session (go next):", e)

        real_sid = gdd_session_map.get(session)
        if not real_sid:
            await ws.send_json({"type": "wizard_notice", "text": "❌ Unable to save GDD answer (no backend session). Try activating the GDD Wizard again."})
            return

        async with httpx.AsyncClient() as client:
            await client.post("http://localhost:8000/gdd/answer", json={"session_id": real_sid, "answer": text})

        await ws.send_json({"type": "wizard_answer", "text": text})

        from app.gdd_engine.gdd_questions import QUESTIONS
        stage = gdd_wizard_stage.get(session, 0) + 1

        if stage >= len(QUESTIONS):
            await ws.send_json({"type": "wizard_notice", "text": "🎉 All questions answered! Say **Finish GDD** to generate it."})
            return

        gdd_wizard_stage[session] = stage
        await ws.send_json({"type": "wizard_question", "text": QUESTIONS[stage], "voice": QUESTIONS[stage]})

        async def _enqueue():
            cleaned = clean_sentence_for_tts(QUESTIONS[stage])
            if cleaned:
                tts_sentence_queue[session].append(cleaned)
                task = asyncio.create_task(async_tts(cleaned))
                tts_gen_tasks[session].append(task)
                if session not in tts_playback_task or tts_playback_task[session].done():
                    tts_playback_task[session] = asyncio.create_task(tts_playback_worker(session))

        asyncio.get_event_loop().create_task(_enqueue())
        return

    # If still in wizard but not covered above, treat as wizard answer
    if gdd_wizard_active.get(session, False):
        async with httpx.AsyncClient() as client:
            await client.post("http://localhost:8000/gdd/answer", json={"session_id": session, "answer": text})

        await ws.send_json({"type": "wizard_answer", "text": text})
        return

    # Normal LLM flow (non-wizard)
    await ws.send_json({"type": "final", "text": text})
    llm_stop_flags[session] = False

    async for token in stream_llm(text):
        if llm_stop_flags[session]:
            break
        await ws.send_json({"type": "llm_stream", "token": token})

    await ws.send_json({"type": "llm_done"})


async def azure_stream(ws: WebSocket):
    """
    Main entry that manages STT recognizer and websocket message loop.
    This function is adapted from the original main.py and uses session_state + tts_engine helpers.
    """
    session = str(uuid.uuid4())
    print("WS connected:", session)

    llm_stop_flags[session] = False
    user_last_input_was_voice[session] = False
    sentence_buffer[session] = ""
    ensure_structs(session)

    playback_ws_registry[session] = ws

    push_stream = speechsdk.audio.PushAudioInputStream(
        stream_format=speechsdk.audio.AudioStreamFormat(
            samples_per_second=16000, bits_per_sample=16, channels=1
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

    # Partial STT callback
    def recognizing(evt):
        text = (evt.result.text or "").strip()
        if text:
            asyncio.run_coroutine_threadsafe(ws.send_json({"type": "partial", "text": text}), loop)

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

    # Final STT callback
    def recognized(evt):
        if evt.result.reason != speechsdk.ResultReason.RecognizedSpeech:
            return

        raw_text = evt.result.text.strip()
        if not raw_text or raw_text in [".", "uh", "um"]:
            print("⚠ Ignoring garbage:", raw_text)
            return

        print("🟢 Final STT:", raw_text)
        QUESTIONS = load_questions()

        # Normalize STT:
        lower = raw_text.lower().strip()
        lower = lower.replace(".", "").replace("?", "").replace("!", "")
        lower = re.sub(r"\s+", " ", lower)

        normalized = lower
        normalized = normalized.replace("g d d", "gdd")
        normalized = normalized.replace("g d", "gd")
        normalized = normalized.replace("g. d. d.", "gdd")
        normalized = normalized.strip()

        user_last_input_was_voice[session] = True
        sentence_buffer[session] = ""

        # Wizard activation via voice (unified)
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

        if any(re.search(rf"\b{re.escape(p)}\b", normalized) for p in activation_phrases):
            print("🎯 WIZARD ACTIVATED (voice):", normalized)
            gdd_wizard_active[session] = True
            gdd_wizard_stage[session] = 0

            # create server-side GDD session (non-blocking best-effort)
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
                        except Exception as e:
                            print("❌ Failed to send gdd_session_id:", e)
                    else:
                        print("⚠ /gdd/start failed:", res.status_code, res.text)
                except Exception as e:
                    print("❌ Exception calling /gdd/start:", e)

            asyncio.run_coroutine_threadsafe(_start_gdd_session(), loop)

            asyncio.run_coroutine_threadsafe(ws.send_json({"type": "wizard_notice",
                            "text": "🎮 **GDD Wizard Activated!**\nSay **Go Next** anytime to proceed."}), loop)

            if QUESTIONS:
                asyncio.run_coroutine_threadsafe(ws.send_json({
                    "type": "wizard_question",
                    "text": f"{QUESTIONS[0]}",
                    "voice": f"{QUESTIONS[0]}"
                }), loop)

                async def _enqueue_first_question():
                    cleaned = clean_sentence_for_tts(QUESTIONS[0])
                    if cleaned:
                        tts_sentence_queue[session].append(cleaned)
                        task = asyncio.create_task(async_tts(cleaned))
                        tts_gen_tasks[session].append(task)
                        if session not in tts_playback_task or tts_playback_task[session].done():
                            tts_playback_task[session] = asyncio.create_task(tts_playback_worker(session))

                asyncio.run_coroutine_threadsafe(_enqueue_first_question(), loop)

            asyncio.run_coroutine_threadsafe(ws.send_json({"type": "final", "text": raw_text}), loop)
            return

        # Go next voice command
        if gdd_wizard_active.get(session, False) and ("go next" in normalized or normalized == "next"):
            stage = gdd_wizard_stage[session] + 1
            if stage >= len(QUESTIONS):
                asyncio.run_coroutine_threadsafe(ws.send_json({
                    "type": "wizard_notice",
                    "text": "🎉 All questions answered! Say **Finish GDD**."
                }), loop)
                return

            gdd_wizard_stage[session] = stage
            asyncio.run_coroutine_threadsafe(ws.send_json({
                "type": "wizard_question",
                "text": f"{QUESTIONS[stage]}",
                "voice": f"{QUESTIONS[stage]}"
            }), loop)

            async def _enqueue_next_question(qtext):
                cleaned = clean_sentence_for_tts(qtext)
                if cleaned:
                    tts_sentence_queue[session].append(cleaned)
                    task = asyncio.create_task(async_tts(cleaned))
                    tts_gen_tasks[session].append(task)
                    if session not in tts_playback_task or tts_playback_task[session].done():
                        tts_playback_task[session] = asyncio.create_task(tts_playback_worker(session))

            asyncio.run_coroutine_threadsafe(_enqueue_next_question(QUESTIONS[stage]), loop)
            return

        # Finish GDD voice
        if gdd_wizard_active.get(session, False) and re.search(r"\b(finish gdd|generate gdd|complete gdd)\b", normalized):
            print("🎯 FINISH GDD triggered:", normalized)

            async def _finish():
                try:
                    gdd_sid = gdd_session_map.get(session)
                    if not gdd_sid:
                        await ws.send_json({"type": "wizard_notice", "text": "❌ No GDD session found — nothing to finish."})
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
                        await ws.send_json({"type": "wizard_notice", "text": "📘 **Your GDD is ready! Say Download GDD to Downnload it**"})
                        await ws.send_json({"type": "final", "text": data.get("markdown", "")})
                    else:
                        await ws.send_json({"type": "wizard_notice", "text": f"❌ Error generating GDD (status {res.status_code})."})
                except Exception as e:
                    print("❌ ERROR inside _finish():", e)
                    try:
                        await ws.send_json({"type": "wizard_notice", "text": f"❌ Exception generating GDD: {e}"})
                    except: pass
                finally:
                    gdd_wizard_active[session] = False

            asyncio.run_coroutine_threadsafe(_finish(), loop)
            return

        # Wizard answer (voice) — treat as answer if not a command
        if gdd_wizard_active.get(session, False):
            cmd_phrases = [
                "go next", "next question", "next",
                "finish gdd", "finish", "complete gdd",
                "activate gdd", "activate wizard",
                "call next"
            ]

            if any(phrase in normalized for phrase in cmd_phrases):
                print("🛑 SKIP ANSWER (voice command detected):", raw_text)
            else:
                if len(raw_text.split()) < 3:
                    print("🛑 SKIP short/noisy fragment:", raw_text)
                    return

                async def _record():
                    try:
                        gdd_sid = gdd_session_map.get(session)
                        print("🔍 EXPORT CHECK — gdd_session_map:", gdd_session_map)
                        if not gdd_sid:
                            print("❌ No GDD SID for answer")
                            return

                        print("💾 Saving REAL answer:", raw_text)
                        async with httpx.AsyncClient(timeout=10) as client:
                            await client.post("http://localhost:8000/gdd/answer", json={"session_id": gdd_sid, "answer": raw_text})
                        await ws.send_json({"type": "wizard_answer", "text": raw_text})
                    except Exception as e:
                        print("❌ /gdd/answer failed:", e)

                asyncio.run_coroutine_threadsafe(_record(), loop)
                return

        # Export GDD voice command
        if ("export gdd" in normalized or "export the gdd" in normalized or "download gdd" in normalized or "export document" in normalized):
            gdd_sid = gdd_session_map.get(session)
            if not gdd_sid:
                asyncio.run_coroutine_threadsafe(ws.send_json({"type": "wizard_notice", "text": "❌ No GDD available to export. Please finish GDD first."}), loop)
                return

            print("🎯 EXPORT GDD triggered:", normalized)

            async def _export():
                print(f"📡 Calling /gdd/export for gdd_sid: {gdd_sid}")
                async with httpx.AsyncClient() as client:
                    res = await client.post("http://localhost:8000/gdd/export", json={"session_id": gdd_sid})

                if res.status_code != 200:
                    await ws.send_json({"type": "wizard_notice", "text": f"❌ Export failed ({res.status_code})."})
                    return

                await ws.send_json({"type": "gdd_export_ready", "filename": f"GDD_{gdd_sid}.docx"})

            asyncio.run_coroutine_threadsafe(_export(), loop)
            return

        # Normal LLM flow (unchanged behaviour)
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


    # wire up sdk callbacks
    recognizer.recognizing.connect(recognizing)
    recognizer.recognized.connect(recognized)
    recognizer.start_continuous_recognition_async().get()

    print("🎤 Azure STT started successfully")

    # -------------------------------
    # MAIN WS LOOP — receives bytes & text messages
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
