# app/voice/tts_engine.py
from pathlib import Path
import asyncio
import logging
import azure.cognitiveservices.speech as speechsdk
from app.config import CONFIG

log = logging.getLogger("app.voice.tts_engine")
log.setLevel(logging.INFO)

AZURE_SPEECH_KEY = CONFIG["AZURE_SPEECH_KEY"]
AZURE_SPEECH_REGION = CONFIG["AZURE_SPEECH_REGION"]

# Load SSML template (make sure file exists) — ensure UTF-8
SSML_PATH = Path("app/voice/nyra_ssml.xml")
if not SSML_PATH.exists():
    log.warning("NYRA SSML template not found at %s", SSML_PATH)
SSML_TEMPLATE = SSML_PATH.read_text(encoding="utf-8")

log.info("Loaded NYRA SSML template from %s", SSML_PATH)

# TTS config — use the Indian voice you placed in SSML (Neerja)
speech_tts_config = speechsdk.SpeechConfig(
    subscription=AZURE_SPEECH_KEY,
    region=AZURE_SPEECH_REGION
)

# Set default voice name (keep consistent with SSML). This helps the SDK choose
# the correct voice if SSML doesn't specify it.
speech_tts_config.speech_synthesis_voice_name = "en-IN-NeerjaNeural"

# We'll return raw 16k PCM (what your playback worker expects)
speech_tts_config.set_speech_synthesis_output_format(
    speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm
)


def escape_xml(s: str) -> str:
    # Minimal XML escaping for SSML injection safety
    if s is None:
        return ""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace("\"", "&quot;")
         .replace("'", "&apos;")
    )


def build_ssml(text_en: str, text_hi: str = "") -> str:
    """
    Fill the NYRA SSML template. If not using Hinglish, leave text_hi empty.
    """
    try:
        ssml = SSML_TEMPLATE.replace("{{TEXT_EN}}", escape_xml(text_en))
        ssml = ssml.replace("{{TEXT_HI}}", escape_xml(text_hi or ""))
        return ssml
    except Exception as e:
        log.exception("Failed to build SSML: %s", e)
        # fallback to minimal SSML
        return f'<speak><voice name="en-IN-NeerjaNeural">{escape_xml(text_en)}</voice></speak>'


def azure_tts_generate_sync_try(ssml: str) -> bytes:
    """
    Low-level helper: attempts speak_ssml_async and returns bytes or b''.
    It logs cancellation details for debugging.
    """
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_tts_config,
        audio_config=None
    )

    try:
        result = synthesizer.speak_ssml_async(ssml).get()
    except Exception as exc:
        log.exception("Exception when calling speak_ssml_async: %s", exc)
        return b""

    # Successful synthesis
    if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
        return result.audio_data

    # Cancellation — inspect details
    if result.reason == speechsdk.ResultReason.Canceled:
        details = result.cancellation_details
        reason = getattr(details, "reason", None)
        error_details = getattr(details, "error_details", None)
        log.error("Azure TTS failed. Reason: %s | Details: %s | Error details: %s", reason, details, error_details)
    else:
        log.error("Azure TTS unexpected result reason: %s", result.reason)

    return b""


def azure_tts_generate_sync(ssml: str) -> bytes:
    """
    Robust wrapper:
      1) Try the full template SSML.
      2) If canceled, try a minimal SSML wrapper.
      3) If still canceled, fallback to speak_text_async.
    Returns raw PCM bytes or b'' on final failure.
    """
    # 1) Try using provided SSML
    audio = azure_tts_generate_sync_try(ssml)
    if audio:
        return audio

    # 2) Try a minimal SSML wrapper (simple voice + text) in case template uses unsupported tags
    minimal_ssml = f'<speak><voice name="en-IN-NeerjaNeural">{escape_xml(strip_ssml_placeholder_text(ssml))}</voice></speak>'
    log.info("Primary SSML failed; trying minimal SSML.")
    audio = azure_tts_generate_sync_try(minimal_ssml)
    if audio:
        return audio

    # 3) Fallback: try plain text synthesis (speak_text_async)
    try:
        synthesizer = speechsdk.SpeechSynthesizer(
            speech_config=speech_tts_config,
            audio_config=None
        )
        log.info("Minimal SSML failed; falling back to text-to-speech (speak_text_async).")
        result = synthesizer.speak_text_async(strip_ssml_placeholder_text(ssml)).get()
        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            return result.audio_data
        if result.reason == speechsdk.ResultReason.Canceled:
            details = result.cancellation_details
            log.error("Fallback speak_text_async canceled: %s | %s", getattr(details, "reason", None), getattr(details, "error_details", None))
    except Exception as e:
        log.exception("Exception during speak_text_async fallback: %s", e)

    return b""


def strip_ssml_placeholder_text(ssml: str) -> str:
    """
    Attempt to extract plaintext from an ssml input (simple heuristic).
    If input is already plaintext this returns it unchanged.
    """
    try:
        # crude: remove tags
        import re
        text = re.sub(r"<[^>]+>", "", ssml)
        # collapse whitespace
        text = " ".join(text.split())
        return text.strip()
    except Exception:
        return ssml


async def async_tts(text: str, text_hi: str = "") -> bytes:
    """
    Async wrapper: build SSML and synthesize on a thread.
    Returns raw PCM bytes (16kHz, 16-bit, mono).
    """
    ssml = build_ssml(text, text_hi)
    return await asyncio.to_thread(azure_tts_generate_sync, ssml)


# Convenience function for debug route
def synthesize_test_phrase(phrase: str = "Hello from NYRA — test.") -> bytes:
    ssml = build_ssml(phrase)
    return azure_tts_generate_sync(ssml)
