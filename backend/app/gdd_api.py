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

router = APIRouter()
session_mgr = SessionManager()

# --------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------
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

class NextRequest(BaseModel):
    session_id: str

class ExportBySessionRequest(BaseModel):
    session_id: str


# --------------------------------------------------------------------
# /api/orchestrate — unchanged
# --------------------------------------------------------------------
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
        raise HTTPException(400, str(e))


# --------------------------------------------------------------------
# /export-docx — unchanged
# --------------------------------------------------------------------
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
        raise HTTPException(500, str(e))


# --------------------------------------------------------------------
# /start — return first question
# --------------------------------------------------------------------
@router.post("/start")
async def gdd_start():
    session_id = session_mgr.create_session()

    session_mgr._store[session_id]["answers"] = []
    session_mgr._store[session_id]["index"] = 0

    return {
        "status": "ok",
        "session_id": session_id,
        "question": QUESTIONS[0],
        "index": 0,
        "total": len(QUESTIONS)
    }


# --------------------------------------------------------------------
# /next — return next question
# --------------------------------------------------------------------
@router.post("/next")
async def gdd_next(req: NextRequest):
    session_id = req.session_id
    if not session_mgr.session_exists(session_id):
        raise HTTPException(404, "Session not found")

    data = session_mgr._store[session_id]
    index = data.get("index", 0)
    total = len(QUESTIONS)

    # End of list → wizard completed
    if index >= total:
        return {"status": "done"}

    question = QUESTIONS[index]

    # Advance pointer
    data["index"] = index + 1

    return {
        "status": "ok",
        "question": question,
        "index": index,
        "total": total
    }


# --------------------------------------------------------------------
# /answer — save answer along with the corresponding question
# --------------------------------------------------------------------
@router.post("/answer")
async def gdd_answer(payload: AnswerInput):
    session_id = payload.session_id
    if not session_mgr.session_exists(session_id):
        raise HTTPException(404, "Session not found")

    raw = payload.answer.strip()
    data = session_mgr._store[session_id]

    index = data.get("index", 0)
    total = len(QUESTIONS)

    # If user answers after wizard finished
    if index == 0:
        q_index = 0
    else:
        q_index = index - 1

    if q_index >= total:
        return {"status": "done"}

    answers = data.get("answers", [])

    # Ensure structure
    while len(answers) <= q_index:
        answers.append({"question": QUESTIONS[len(answers)], "answer": ""})

    # Append or overwrite cleanly
    if answers[q_index]["answer"]:
        answers[q_index]["answer"] += "\n" + raw
    else:
        answers[q_index]["answer"] = raw

    data["answers"] = answers

    return {"status": "ok", "recorded_for": q_index}


# --------------------------------------------------------------------
# /finish — build concept safely
# --------------------------------------------------------------------
@router.post("/finish")
async def gdd_finish(payload: FinishInput):
    session_id = payload.session_id
    if not session_mgr.session_exists(session_id):
        raise HTTPException(404, "Session not found")

    data = session_mgr._store[session_id]
    answers = data.get("answers", [])

    # ---------- FIXED KEY ERROR ----------
    concept_parts = []
    for qa in answers:
        q = qa.get("question")
        a = qa.get("answer", "").strip()
        if not q or not a:
            continue
        concept_parts.append(f"{q}\n{a}")

    concept = "\n\n".join(concept_parts)
    if not concept.strip():
        concept = "No meaningful answers were provided."

    orchestrator = GDDOrchestrator(concept)
    results = orchestrator.run_pipeline()

    markdown = results["integration"]["markdown"]
    data["markdown"] = markdown

    return {
        "status": "ok",
        "session_id": session_id,
        "concept": concept,
        "results": results,
        "markdown": markdown,
        "export_available": True
    }


# --------------------------------------------------------------------
# /export-by-session — unchanged
# --------------------------------------------------------------------
@router.post("/export-by-session")
async def gdd_export_session(payload: ExportBySessionRequest):
    session_id = payload.session_id
    if not session_mgr.session_exists(session_id):
        raise HTTPException(404, "Session not found")

    markdown = session_mgr._store[session_id].get("markdown")
    if not markdown:
        raise HTTPException(400, "GDD not generated yet.")

    filename = f"gdd_{uuid.uuid4().hex}.docx"
    out_path = os.path.join(tempfile.gettempdir(), filename)
    export_to_docx(markdown, out_path)

    return FileResponse(
        out_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="Game_Design_Document.docx"
    )


# --------------------------------------------------------------------
# /export — final voice export
# --------------------------------------------------------------------
@router.post("/export")
async def export_gdd(payload: dict):
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(400, "Missing session_id")

    if not session_mgr.session_exists(session_id):
        raise HTTPException(404, "Invalid session_id")

    markdown = session_mgr._store[session_id].get("markdown")
    if not markdown:
        raise HTTPException(400, "No generated markdown. Say 'Finish GDD' first.")

    tmp_path = os.path.join(tempfile.gettempdir(), f"GDD_{session_id}.docx")
    export_to_docx(markdown, tmp_path)

    return FileResponse(
        tmp_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"GDD_{session_id}.docx"
    )
