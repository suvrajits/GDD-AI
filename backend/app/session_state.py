# app/session_state.py
import asyncio
from typing import Dict, List
from fastapi import WebSocket

# Global session/state dictionaries (moved from main.py)
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

# TTS constants
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
MIN_PADDING = 0.02
MAX_PADDING = 0.08

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
