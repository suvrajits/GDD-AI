# backend/app/gdd_engine/session_manager.py

import uuid
from typing import Dict, Any, List
from .gdd_questions import QUESTIONS

_GDD_SESSIONS: Dict[str, Dict[str, Any]] = {}


class SessionManager:
    def __init__(self):
        self._store = _GDD_SESSIONS

    def create_session(self) -> str:
        session_id = str(uuid.uuid4())
        self._store[session_id] = {
            "step": 0,
            "answers": [],
            "completed": False
        }
        return session_id

    def session_exists(self, session_id: str) -> bool:
        return session_id in self._store

    def add_answer(self, session_id: str, answer: str) -> None:
        if session_id not in self._store:
            raise KeyError(f"Session '{session_id}' not found.")

        session = self._store[session_id]
        if session["completed"]:
            return

        step = session["step"]
        question = QUESTIONS[step] if step < len(QUESTIONS) else f"q_{step}"

        session["answers"].append({
            "question": question,
            "answer": answer
        })

        session["step"] += 1
        if session["step"] >= len(QUESTIONS):
            session["completed"] = True

    def get_answers(self, session_id: str) -> List[Dict[str, str]]:
        if session_id not in self._store:
            raise KeyError(f"Session '{session_id}' not found.")
        return self._store[session_id]["answers"]

    def get_current_question(self, session_id: str):
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

    # ⭐⭐⭐ FIXED + AUTO-FILL VERSION ⭐⭐⭐
    def build_concept(self, session_id: str) -> str:
        if session_id not in self._store:
            raise KeyError(f"Session '{session_id}' not found.")

        session = self._store[session_id]
        answers = session["answers"]
        total_questions = len(QUESTIONS)

        # Auto-fill missing answers
        if len(answers) < total_questions:
            for i in range(len(answers), total_questions):
                answers.append({
                    "question": QUESTIONS[i],
                    "answer": "(No answer provided)"
                })

        lines = ["Guided GDD inputs:"]
        for idx, qa in enumerate(answers, start=1):
            q = qa["question"]
            a = qa["answer"]
            lines.append(f"{idx}. {q}\nAnswer: {a}\n")

        return "\n".join(lines)

