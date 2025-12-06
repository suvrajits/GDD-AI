# backend/app/gdd_api.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse

from app.gdd_engine.orchestrator.orchestrator import GDDOrchestrator
from app.gdd_engine.docx_exporter import export_to_docx
from app.gdd_engine.session_manager import SessionManager
from app.gdd_engine.gdd_questions import QUESTIONS

import uuid
import os
import tempfile
from docx import Document

router = APIRouter()
session_mgr = SessionManager()

# -----------------------------
# Request Models
# -----------------------------
class GDDRequest(BaseModel):
    concept: str
    pinned_notes: dict | None = None

class ExportRequest(BaseModel):
    markdown: str

class AnswerInput(BaseModel):
    session_id: str
    answer: str

class FinishInput(BaseModel):
    session_id: str

class ExportBySessionRequest(BaseModel):
    session_id: str


# -----------------------------
# /api/orchestrate
# -----------------------------
@router.post("/api/orchestrate")
async def orchestrate_gdd(payload: GDDRequest):
    try:
        orchestrator = GDDOrchestrator(payload.concept)
        results = orchestrator.run_pipeline()

        return {
            "status": "ok",
            "concept": payload.concept,
            "results": results,
            "markdown": results["integration"]["markdown"],
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# -----------------------------
# /export-docx (manual button)
# -----------------------------
@router.post("/export-docx")
async def export_docx(req: ExportRequest):
    try:
        filename = f"gdd_{uuid.uuid4().hex}.docx"
        out_path = os.path.join(tempfile.gettempdir(), filename)

        export_to_docx(req.markdown, out_path)

        return FileResponse(
            out_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="gdd_output.docx",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------
# /start
# -----------------------------
@router.post("/start")
async def gdd_start():
    session_id = session_mgr.create_session()

    if not QUESTIONS:
        raise HTTPException(status_code=500, detail="QUESTIONS list is empty.")

    return {
        "status": "ok",
        "session_id": session_id,
        "question": QUESTIONS[0],
        "index": 0,
        "total": len(QUESTIONS),
    }


# -----------------------------
# /answer
# -----------------------------
@router.post("/answer")
async def gdd_answer(payload: AnswerInput):
    session_id = payload.session_id
    if not session_mgr.session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    raw = payload.answer.strip()
    text = raw.lower()
    answers = session_mgr.get_answers(session_id)
    idx = len(answers)

    # NEXT
    if text in ("next", "go next", "next question", "continue", "done"):
        if idx >= len(QUESTIONS):
            return {
                "status": "done",
                "message": "All questions answered. Call /finish to generate the GDD.",
            }
        if idx == 0 or len(answers) == idx:
            session_mgr.add_answer(session_id, "")
        return {
            "status": "ok",
            "question": QUESTIONS[idx],
            "index": idx,
            "total": len(QUESTIONS),
        }

    # Brainstorming
    if idx == 0 or len(answers) == idx:
        session_mgr.add_answer(session_id, raw)
    else:
        combined = answers[-1]["answer"] + f"\n{raw}"
        answers[-1]["answer"] = combined

    stay_index = idx - 1 if idx > 0 else 0
    return {
        "status": "stay",
        "message": "Noted. Continue brainstorming or say 'next' to continue.",
        "question": QUESTIONS[stay_index],
        "index": stay_index,
        "total": len(QUESTIONS),
    }


# -----------------------------
# /finish  (GENERATES & STORES FINAL MARKDOWN)
# -----------------------------
@router.post("/finish")
async def gdd_finish(payload: FinishInput):
    session_id = payload.session_id
    if not session_mgr.session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    concept = session_mgr.build_concept(session_id)
    orchestrator = GDDOrchestrator(concept)
    results = orchestrator.run_pipeline()

    markdown = results["integration"]["markdown"]

    # ⭐ FIX — store inside _store, not nonexistent .sessions
    session_mgr._store[session_id]["markdown"] = markdown  # <-- FIXED

    return {
        "status": "ok",
        "session_id": session_id,
        "concept": concept,
        "results": results,
        "markdown": markdown,
        "export_available": True
    }


# -----------------------------
# /export-by-session  (UI button)
# -----------------------------
@router.post("/export-by-session")
async def gdd_export_session(payload: ExportBySessionRequest):

    session_id = payload.session_id
    if not session_mgr.session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    # ⭐ FIX — use _store instead of .sessions
    markdown = session_mgr._store[session_id].get("markdown")  # <-- FIXED
    if not markdown:
        raise HTTPException(status_code=400, detail="GDD not generated yet. Call /finish first.")

    filename = f"gdd_{uuid.uuid4().hex}.docx"
    out_path = os.path.join(tempfile.gettempdir(), filename)

    export_to_docx(markdown, out_path)

    return FileResponse(
        out_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="Game_Design_Document.docx",
    )


# -----------------------------
# /export   (VOICE + UI FINAL → NO RE-GENERATION)
# -----------------------------
@router.post("/export")
async def export_gdd(payload: dict):

    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="Missing session_id")

    if not session_mgr.session_exists(session_id):
        raise HTTPException(status_code=404, detail="Invalid session_id")

    # ⭐ FIX — use saved markdown
    markdown = session_mgr._store[session_id].get("markdown")  # <-- FIXED
    if not markdown:
        raise HTTPException(status_code=400, detail="GDD not generated yet. Say 'Finish GDD' first.")

    tmp_path = os.path.join(tempfile.gettempdir(), f"GDD_{session_id}.docx")

    export_to_docx(markdown, tmp_path)

    return FileResponse(
        tmp_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"GDD_{session_id}.docx"
    )
