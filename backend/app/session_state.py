# app/session_state.py 
# -------------------------------------------------------------
#  Unified state store for streaming, wizard flow, and GDD data
# -------------------------------------------------------------

import asyncio
from typing import Dict, List
from fastapi import WebSocket
from app.gdd_engine.gdd_questions import QUESTIONS


# ============================================================
#  GLOBAL STATE — STREAMING + WIZARD (unchanged from your file)
# ============================================================

llm_stop_flags: Dict[str, bool] = {}
user_last_input_was_voice: Dict[str, bool] = {}
sentence_buffer: Dict[str, str] = {}

# -------------------------
# TTS STATE
# -------------------------
tts_sentence_queue: Dict[str, List[str]] = {}
tts_gen_tasks: Dict[str, List[asyncio.Task]] = {}
tts_cancel_events: Dict[str, asyncio.Event] = {}
tts_playback_task: Dict[str, asyncio.Task] = {}

playback_ws_registry: Dict[str, WebSocket] = {}
assistant_is_speaking: Dict[str, bool] = {}

# -------------------------
# WIZARD STATE
# -------------------------
gdd_wizard_active: Dict[str, bool] = {}       # session → bool
gdd_session_map: Dict[str, str] = {}          # session → backend session_id

# TTS constants
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
MIN_PADDING = 0.02
MAX_PADDING = 0.08


# ============================================================
#  REQUIRED: ENSURE PER-SESSION STRUCTURES
# ============================================================
def ensure_structs(session: str):
    """Initialize all per-session dictionaries."""
    tts_sentence_queue.setdefault(session, [])
    tts_gen_tasks.setdefault(session, [])
    tts_cancel_events.setdefault(session, asyncio.Event())
    assistant_is_speaking.setdefault(session, False)

    gdd_wizard_active.setdefault(session, False)
    gdd_session_map.setdefault(session, None)

    llm_stop_flags.setdefault(session, False)
    user_last_input_was_voice.setdefault(session, False)
    sentence_buffer.setdefault(session, "")


# ============================================================
#  CANCEL TTS FOR A SESSION
# ============================================================
def cancel_tts_generation(session: str):
    """Cancel pending TTS tasks and reset queue for this session."""
    ev = tts_cancel_events.get(session)
    if ev:
        ev.set()

    # Cancel all tasks
    for task in tts_gen_tasks.get(session, []):
        if not task.done():
            try:
                task.cancel()
            except:
                pass

    # Reset TTS buffers
    tts_sentence_queue[session] = []
    tts_gen_tasks[session] = []


# ============================================================
#  NEW: GDD SESSION MANAGER (required for patched gdd_api.py)
# ============================================================
class SessionManager:
    """
    Stores each GDD session:
        {
            "answers": [
                {"question": "...", "answer": "..."},
                ...
            ],
            "index": <int>,
            "markdown": <str or None>
        }
    """

    def __init__(self):
        self._store: Dict[str, dict] = {}

    # -------------------------------------------------------
    def create_session(self) -> str:
        import uuid
        sid = str(uuid.uuid4())

        self._store[sid] = {
            "answers": [],
            "index": 0,         # next question index to ask
            "markdown": None
        }
        return sid

    # -------------------------------------------------------
    def session_exists(self, sid: str) -> bool:
        return sid in self._store

    # -------------------------------------------------------
    def add_answer(self, sid: str, answer: str):
        """Store answer aligned to correct question."""
        data = self._store.get(sid)
        if data is None:
            return

        total = len(QUESTIONS)
        idx = data.get("index", 0)

        # User answers previous question
        q_index = max(0, idx - 1)
        if q_index >= total:
            q_index = total - 1

        answers = data.get("answers", [])

        # Grow answer list if needed
        while len(answers) <= q_index:
            answers.append({
                "question": QUESTIONS[len(answers)],
                "answer": ""
            })

        # Append or set answer
        if answers[q_index]["answer"]:
            answers[q_index]["answer"] += "\n" + answer
        else:
            answers[q_index]["answer"] = answer

        data["answers"] = answers

    # -------------------------------------------------------
    def get_answers(self, sid: str):
        return self._store.get(sid, {}).get("answers", [])

    # -------------------------------------------------------
    def get_markdown(self, sid: str):
        return self._store.get(sid, {}).get("markdown")

    # -------------------------------------------------------
    def set_markdown(self, sid: str, markdown: str):
        if sid in self._store:
            self._store[sid]["markdown"] = markdown

    # -------------------------------------------------------
    def reset_session(self, sid: str):
        """Reset wizard state but keep session ID for export."""
        if sid in self._store:
            self._store[sid]["answers"] = []
            self._store[sid]["index"] = 0
            self._store[sid]["markdown"] = None
