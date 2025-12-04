# app/gdd_api.py

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse

# FIXED IMPORTS — use app.gdd_engine.*
from app.gdd_engine.orchestrator.orchestrator import GDDOrchestrator
from app.gdd_engine.docx_exporter import export_to_docx
from app.gdd_engine.session_manager import SessionManager
from app.gdd_engine.gdd_questions import QUESTIONS
import uuid


router = APIRouter()
session_mgr = SessionManager()


# ---------------------------------------------------------
# Request Models
# ---------------------------------------------------------
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


# ---------------------------------------------------------
# POST /api/orchestrate
# ---------------------------------------------------------
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


# ---------------------------------------------------------
# POST /export-docx
# ---------------------------------------------------------
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


# ---------------------------------------------------------
# Guided Mode — /start
# ---------------------------------------------------------
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


# ---------------------------------------------------------
# Guided Mode — /answer
# ---------------------------------------------------------
@router.post("/answer")
async def gdd_answer(payload: AnswerInput):
    if not session_mgr.session_exists(payload.session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    session_mgr.add_answer(payload.session_id, payload.answer)

    answers = session_mgr.get_answers(payload.session_id)
    idx = len(answers)

    if idx >= len(QUESTIONS):
        return {
            "status": "done",
            "message": "All questions answered. Call /finish to generate the GDD.",
        }

    return {
        "status": "ok",
        "question": QUESTIONS[idx],
        "index": idx,
        "total": len(QUESTIONS),
    }


# ---------------------------------------------------------
# Guided Mode — /finish
# ---------------------------------------------------------
@router.post("/finish")
async def gdd_finish(payload: FinishInput):
    if not session_mgr.session_exists(payload.session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    answers = session_mgr.get_answers(payload.session_id)
    if len(answers) < len(QUESTIONS):
        raise HTTPException(status_code=400, detail="Not all questions answered yet.")

    # Build final concept string
    concept_text = session_mgr.build_concept(payload.session_id)

    # Run orchestrator
    orchestrator = GDDOrchestrator(concept_text)
    results = orchestrator.run_pipeline()
    markdown = results["integration"]["markdown"]

    return {
        "status": "ok",
        "concept": concept_text,
        "results": results,
        "markdown": markdown,
    }
