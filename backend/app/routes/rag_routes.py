# app/routes/rag_routes.py

import os
from fastapi import APIRouter, UploadFile, File, HTTPException
from typing import List
from pathlib import Path

# Correct import (NO rag import)
from app.rag_engine import RAGEngine

router = APIRouter()

UPLOAD_DIR = Path("./data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# instantiate RAG engine (FAISS + Azure embeddings)
rag = RAGEngine(
    index_dir="./data/faiss_index",
    embedding_dim=1536,
    batch_size=64,
    max_chunks_per_file=300
)


@router.post("/rag/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    saved_files = []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in [".html", ".htm", ".txt", ".md"]:
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")
        path = UPLOAD_DIR / f.filename
        with open(path, "wb") as out:
            out.write(await f.read())
        saved_files.append(str(path))
    return {"message": "Files uploaded", "files": saved_files}

@router.post("/rag/ingest")
async def ingest_uploaded_files():
    files = list(UPLOAD_DIR.glob("*"))
    if not files:
        raise HTTPException(status_code=400, detail="No uploaded files.")
    paths = [str(p) for p in files]
    # Ingest (this may take a while depending on number of chunks and Azure quota)
    rag.ingest_files(paths)
    # optional: remove uploaded files after ingestion
    for f in files:
        try:
            f.unlink()
        except:
            pass
    return {"message": "Ingestion completed", "files": paths}

@router.get("/rag/search")
async def rag_search(query: str, k: int = 5):
    if not query:
        raise HTTPException(status_code=400, detail="Query cannot be empty.")
    results = rag.search(query, k)
    return {"query": query, "results": results}

@router.get("/rag/list")
async def list_uploaded_files():
    files = [p.name for p in UPLOAD_DIR.glob("*") if p.is_file()]
    return {"files": files}


@router.delete("/rag/file/{filename}")
async def delete_file(filename: str):
    filename = filename.strip()
    success = rag.remove_file(filename)

    if not success:
        return {"message": f"No embeddings found for file {filename}"}

    return {"message": f"File {filename} removed successfully"}

@router.get("/rag/embedded-files")
async def embedded_files():
    # Extract all unique file names from docstore
    files = {
        item["meta"]["file"]
        for item in rag.docstore.values()
        if "file" in item["meta"]
    }
    return {"files": sorted(files)}
