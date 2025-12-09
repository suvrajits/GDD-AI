# app/tts_engine.py
import re
import asyncio
import azure.cognitiveservices.speech as speechsdk

from .session_state import (
    SAMPLE_RATE, BYTES_PER_SAMPLE, MIN_PADDING, MAX_PADDING,
    tts_sentence_queue, tts_gen_tasks, tts_cancel_events, tts_playback_task,
    playback_ws_registry, assistant_is_speaking, ensure_structs, cancel_tts_generation
)

from .config import CONFIG

AZURE_SPEECH_KEY = CONFIG["AZURE_SPEECH_KEY"]
AZURE_SPEECH_REGION = CONFIG["AZURE_SPEECH_REGION"]

# Setup speech config used by sync-generation helper
speech_tts_config = speechsdk.SpeechConfig(
    subscription=AZURE_SPEECH_KEY,
    region=AZURE_SPEECH_REGION
)
speech_tts_config.speech_synthesis_voice_name = "en-IN-NeerjaNeural"
speech_tts_config.set_speech_synthesis_output_format(
    speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm
)

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

def azure_tts_generate_sync(text: str) -> bytes:
    """
    Synchronous Azure TTS call returning raw pcm bytes (same behavior as original).
    Runs in threadpool when used via async_tts.
    """
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

async def tts_playback_worker(session: str):
    """
    Worker that sends queued PCM audio bytes via the websocket registry.
    Mirrors the original playback logic but lives here.
    """
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

            # Always announce sentence_start for wizard TTS OR normal TTS
            if sentence_text:
                try:
                    await ws.send_json({"type": "sentence_start", "text": sentence_text})
                except:
                    pass

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
