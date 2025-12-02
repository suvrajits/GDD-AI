# app/routes/rag_routes.py

import os
from fastapi import APIRouter, UploadFile, File, HTTPException
from typing import List
from pathlib import Path

from app.rag_engine import RAGEngine

router = APIRouter()

# Upload directory
UPLOAD_DIR = Path("./data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Single shared RAG engine
rag = RAGEngine(index_dir="./data/faiss_index")


# ------------------------------------------------------------
# Upload files
# ------------------------------------------------------------
@router.post("/rag/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    saved_files = []

    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in [".html", ".htm", ".txt", ".md"]:
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")

        file_path = UPLOAD_DIR / f.filename
        with open(file_path, "wb") as out:
            out.write(await f.read())

        saved_files.append(str(file_path))

    return {"message": "Files uploaded successfully", "files": saved_files}


# ------------------------------------------------------------
# Ingest uploaded files
# ------------------------------------------------------------
@router.post("/rag/ingest")
async def ingest_uploaded_files():
    files = list(UPLOAD_DIR.glob("*"))
    if not files:
        raise HTTPException(status_code=400, detail="No uploaded files to ingest.")

    paths = [str(f) for f in files]
    rag.ingest_files(paths)

    # cleanup after ingestion
    for f in files:
        try:
            f.unlink()
        except:
            pass

    return {
        "message": "Ingestion completed",
        "chunks": len(rag.docstore)
    }


# ------------------------------------------------------------
# Search
# ------------------------------------------------------------
@router.get("/rag/search")
async def rag_search(query: str, k: int = 5):
    if not query:
        raise HTTPException(status_code=400, detail="Query cannot be empty.")

    results = rag.search(query, k=k)
    return {"query": query, "results": results}


# ------------------------------------------------------------
# List uploaded files
# ------------------------------------------------------------
@router.get("/rag/list")
async def list_uploaded_files():
    files = [p.name for p in UPLOAD_DIR.glob("*") if p.is_file()]
    return {"files": files}
