# backend/app/gdd_engine/session_manager.py
"""
SessionManager class for guided GDD creation.
Replaces former function-based module with an object API expected by gdd_api.py.
"""

import uuid
from typing import Dict, Any, List
from .gdd_questions import QUESTIONS

# In-memory sessions store (POC)
_GDD_SESSIONS: Dict[str, Dict[str, Any]] = {}


class SessionManager:
    def __init__(self):
        # using module-level dict so multiple instances share state in POC
        self._store = _GDD_SESSIONS

    def create_session(self) -> str:
        """Create and return a new session_id."""
        session_id = str(uuid.uuid4())
        self._store[session_id] = {
            "step": 0,
            "answers": [],  # keep answers as ordered list
            "completed": False
        }
        return session_id

    def session_exists(self, session_id: str) -> bool:
        return session_id in self._store

    def add_answer(self, session_id: str, answer: str) -> None:
        """Append answer for current question and advance step."""
        if session_id not in self._store:
            raise KeyError(f"Session '{session_id}' not found.")
        session = self._store[session_id]
        if session["completed"]:
            return
        # store answer paired with question for readability (optional)
        step = session["step"]
        question = QUESTIONS[step] if step < len(QUESTIONS) else f"q_{step}"
        session["answers"].append({"question": question, "answer": answer})
        session["step"] += 1
        if session["step"] >= len(QUESTIONS):
            session["completed"] = True

    def get_answers(self, session_id: str) -> List[Dict[str, str]]:
        """Return a list of {question, answer} dicts in order."""
        if session_id not in self._store:
            raise KeyError(f"Session '{session_id}' not found.")
        return self._store[session_id]["answers"]

    def get_current_question(self, session_id: str):
        """Return the current question text or None if finished."""
        if session_id not in self._store:
            raise KeyError(f"Session '{session_id}' not found.")
        step = self._store[session_id]["step"]
        if step >= len(QUESTIONS):
            return None
        return QUESTIONS[step]

    def is_completed(self, session_id: str) -> bool:
        if session_id not in self._store:
            raise KeyError(f"Session '{session_id}' not found.")
        return self._store[session_id]["completed"]

    def reset_session(self, session_id: str) -> None:
        if session_id in self._store:
            self._store[session_id] = {
                "step": 0,
                "answers": [],
                "completed": False
            }

    def build_concept(self, session_id: str) -> str:
        """
        Produce a combined concept string from the collected answers.
        You can customize formatting here for better orchestrator input.
        """
        if session_id not in self._store:
            raise KeyError(f"Session '{session_id}' not found.")
        answers = self._store[session_id]["answers"]
        lines = ["Guided GDD inputs:"] 
        for idx, qa in enumerate(answers, start=1):
            q = qa.get("question", f"Q{idx}")
            a = qa.get("answer", "")
            lines.append(f"{idx}. {q}\nAnswer: {a}\n")
        return "\n".join(lines)
