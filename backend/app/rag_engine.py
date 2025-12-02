# app/rag_engine.py

"""
RAG Engine (Azure OpenAI + FAISS)
- Extract & clean HTML/TXT
- Chunk text with word overlap
- Embed via Azure OpenAI embedding deployment
- Store normalized vectors in FAISS
- Search via cosine similarity
"""

import os
import re
import pickle
from pathlib import Path
from typing import List, Dict, Any

import numpy as np
from bs4 import BeautifulSoup
from dataclasses import dataclass

from openai import OpenAI
from tqdm import tqdm

try:
    import faiss
except ImportError:
    faiss = None

from .config import CONFIG


# ---------------------------------------
# Chunk dataclass
# ---------------------------------------
@dataclass
class Chunk:
    id: str
    text: str
    source: str
    meta: Dict[str, Any]


# ---------------------------------------
# RAG Engine
# ---------------------------------------
class RAGEngine:
    def __init__(
        self,
        index_dir: str = "./data/faiss_index",
        embedding_dim: int = 1536,   # text-embedding-ada-002 dimension
        chunk_size: int = 800,
        chunk_overlap: int = 40,
    ):
        if faiss is None:
            raise RuntimeError("FAISS is not installed. Install faiss-cpu.")

        # Directories
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        # Config values
        self.embedding_dim = embedding_dim
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

        # -------------------------
        # Azure OpenAI configuration
        # -------------------------
        self.azure_embedding_deployment = CONFIG["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"]
        self.azure_openai_endpoint = CONFIG["AZURE_OPENAI_ENDPOINT"]
        self.azure_openai_key = CONFIG["AZURE_OPENAI_API_KEY"]

        # Azure client
        self.client = OpenAI(
            api_key=self.azure_openai_key,
            base_url=f"{self.azure_openai_endpoint}/openai",
        )

        # Paths
        self.index_path = self.index_dir / "faiss.index"
        self.docstore_path = self.index_dir / "docstore.pkl"

        # In-memory stores
        self.docstore: Dict[str, Any] = {}
        self.index = None

        # Load FAISS + docstore
        self._load_index()

    # ---------------------------------------
    # Load / Save FAISS & Docstore
    # ---------------------------------------
    def _create_faiss_index(self):
        return faiss.IndexFlatIP(self.embedding_dim)

    def _load_index(self):
        if self.index_path.exists():
            try:
                self.index = faiss.read_index(str(self.index_path))
            except:
                self.index = None

        if self.docstore_path.exists():
            try:
                with open(self.docstore_path, "rb") as f:
                    self.docstore = pickle.load(f)
            except:
                self.docstore = {}

        if self.index is None:
            self.index = self._create_faiss_index()

    def _save_index(self):
        faiss.write_index(self.index, str(self.index_path))
        with open(self.docstore_path, "wb") as f:
            pickle.dump(self.docstore, f)

    # ---------------------------------------
    # HTML extraction & cleaning
    # ---------------------------------------
    def _extract_text_from_html(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.extract()

        text = soup.get_text("\n")
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()

    def _clean(self, s: str) -> str:
        s = s.replace("\r", " ")
        s = re.sub(r"[ \t]+", " ", s)
        return s.strip()

    # ---------------------------------------
    # Chunking with word-based overlap
    # ---------------------------------------
    def _chunk_text(self, text: str) -> List[str]:
        text = text.strip()
        if not text:
            return []

        paras = [p.strip() for p in re.split(r"\n{1,}", text) if p.strip()]
        chunks = []
        buf = ""

        for p in paras:
            if len(buf) + len(p) <= self.chunk_size:
                buf = (buf + " " + p).strip()
            else:
                if buf:
                    chunks.append(buf)
                buf = p

        if buf:
            chunks.append(buf)

        # Add overlap
        final_chunks = []
        for i, c in enumerate(chunks):
            if i == 0:
                final_chunks.append(c)
            else:
                prev = chunks[i - 1]
                prev_words = prev.split()
                overlap_words = prev_words[-self.chunk_overlap:]
                overlap = " ".join(overlap_words)
                final_chunks.append((overlap + " " + c).strip())

        return [self._clean(x) for x in final_chunks if x.strip()]

    # ---------------------------------------
    # Embedding
    # ---------------------------------------
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """Embed text list using Azure embedding deployment."""
        resp = self.client.embeddings.create(
            model=self.azure_embedding_deployment,
            input=texts
        )
        return [item.embedding for item in resp.data]

    # ---------------------------------------
    # Ingestion Pipeline
    # ---------------------------------------
    def ingest_files(self, paths: List[str]):
        chunks: List[Chunk] = []

        for p in paths:
            p = Path(p)
            if not p.exists():
                continue

            if p.suffix.lower() in (".html", ".htm"):
                raw = p.read_text("utf-8", errors="ignore")
                txt = self._extract_text_from_html(raw)
            else:
                txt = p.read_text("utf-8", errors="ignore")

            txt = self._clean(txt)
            parts = self._chunk_text(txt)

            for i, c in enumerate(parts):
                chunks.append(
                    Chunk(
                        id=f"{p.name}::{i}",
                        text=c,
                        source=str(p),
                        meta={"file": p.name, "chunk": i},
                    )
                )

        if not chunks:
            print("[RAG] No chunks found for ingestion.")
            return

        print(f"[RAG] Embedding {len(chunks)} chunks…")
        embeddings = self.embed_texts([c.text for c in chunks])

        # Normalize vectors for cosine similarity
        vecs = np.asarray(embeddings, dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1e-9
        vecs = vecs / norms

        # Store in FAISS + docstore
        start_idx = self.index.ntotal
        for i, c in enumerate(chunks):
            key = str(start_idx + i)
            self.docstore[key] = {"text": c.text, "meta": c.meta}

        self.index.add(vecs)
        self._save_index()

        print(f"[RAG] Ingested {len(chunks)} chunks. Total={len(self.docstore)}")

    # ---------------------------------------
    # Semantic Search
    # ---------------------------------------
    def search(self, query: str, k: int = 5):
        q = self._clean(query)

        emb = self.embed_texts([q])[0]
        v = np.array(emb, dtype=np.float32)
        v = v / (np.linalg.norm(v) + 1e-9)
        v = v.reshape(1, -1)

        if self.index.ntotal == 0:
            return []

        scores, ids = self.index.search(v, k)

        results = []
        for score, idx in zip(scores[0], ids[0]):
            item = self.docstore.get(str(idx))
            if not item:
                continue

            results.append({
                "score": float(score),
                "text": item["text"],
                "meta": item["meta"],
            })

        return results
