# backend/app/gdd_api.py

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse

from app.gdd_engine.orchestrator.orchestrator import GDDOrchestrator
from app.gdd_engine.docx_exporter import export_to_docx
from app.gdd_engine.session_manager import SessionManager
from app.gdd_engine.gdd_questions import QUESTIONS

import uuid

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
# /export-docx
# -----------------------------
@router.post("/export-docx")
async def export_docx(req: ExportRequest):
    try:
        filename = f"gdd_{uuid.uuid4().hex}.docx"
        out_path = f"/tmp/{filename}"
        export_to_docx(req.markdown, out_path)

        return FileResponse(
            out_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="gdd_output.docx",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------
# /start (Guided Mode)
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
# Helper: update last answer
# -----------------------------
def update_last_answer(session_id: str, text: str):
    answers = session_mgr.get_answers(session_id)
    if not answers:
        return
    answers[-1] = text
    session_mgr.sessions[session_id]["answers"] = answers


# -----------------------------
# /answer (FINAL, CORRECT VERSION)
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

    # 1) GO NEXT
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

    # 2) Brainstorming (stay in same question)
    if idx == 0 or len(answers) == idx:
        session_mgr.add_answer(session_id, raw)
    else:
        combined = answers[-1] + f"\n{raw}"
        update_last_answer(session_id, combined)

    stay_index = idx - 1 if idx > 0 else 0

    return {
        "status": "stay",
        "message": "Noted. Continue brainstorming or say 'next' to continue.",
        "question": QUESTIONS[stay_index],
        "index": stay_index,
        "total": len(QUESTIONS),
    }


# -----------------------------
# /finish
# -----------------------------
@router.post("/finish")
async def gdd_finish(payload: FinishInput):
    session_id = payload.session_id
    if not session_mgr.session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    answers = session_mgr.get_answers(session_id)
    if len(answers) < len(QUESTIONS):
        raise HTTPException(status_code=400, detail="Not all questions answered yet.")

    concept = session_mgr.build_concept(session_id)

    orchestrator = GDDOrchestrator(concept)
    results = orchestrator.run_pipeline()
    markdown = results["integration"]["markdown"]

    return {
        "status": "ok",
        "concept": concept,
        "results": results,
        "markdown": markdown,
    }
