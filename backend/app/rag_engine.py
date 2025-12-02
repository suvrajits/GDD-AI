# app/rag_engine.py

"""
RAG Engine (Azure OpenAI Embeddings + FAISS)
- Extract & clean HTML/TXT
- Chunk text with word overlap
- Embed via Azure OpenAI embedding deployment (deployment-name)
- Store normalized vectors in FAISS
- Map FAISS row index -> docstore (pickle)
- Search using cosine similarity (IndexFlatIP)
"""

import os
import re
import time
import pickle
from pathlib import Path
from typing import List, Dict, Any

import numpy as np
from bs4 import BeautifulSoup
from dataclasses import dataclass

from openai import OpenAI, RateLimitError
from tqdm import tqdm

try:
    import faiss
except ImportError:
    faiss = None

from .config import CONFIG


@dataclass
class Chunk:
    id: str
    text: str
    source: str
    meta: Dict[str, Any]


class RAGEngine:
    def __init__(
        self,
        index_dir: str = "./data/faiss_index",
        embedding_dim: int = 1536,     # embedding dim for embedding model (check model)
        chunk_size: int = 800,
        chunk_overlap: int = 40,
        batch_size: int = 64,
        api_version: str = "2024-08-01-preview",
        max_chunks_per_file: int = 300,   # split large files into parts of this many chunks
    ):
        if faiss is None:
            raise RuntimeError("FAISS is not installed. Install faiss-cpu.")

        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.embedding_dim = embedding_dim

        self.batch_size = batch_size
        self.api_version = api_version
        self.max_chunks_per_file = max_chunks_per_file

        # Azure config (must be set in CONFIG)
        self.azure_embedding_deployment = CONFIG["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"]
        self.azure_openai_endpoint = CONFIG["AZURE_OPENAI_ENDPOINT"]
        self.azure_openai_key = CONFIG["AZURE_OPENAI_API_KEY"]

        # Build OpenAI client with same pattern as llm_orchestrator (deployment base_url)
        # This uses SDK 2.x style: base_url points to /openai/deployments/<deployment>
        self.client = OpenAI(
            api_key=self.azure_openai_key,
            base_url=f"{self.azure_openai_endpoint}/openai/deployments/{self.azure_embedding_deployment}",
            default_headers={"api-key": self.azure_openai_key},
        )

        # file paths
        self.index_path = self.index_dir / "faiss.index"
        self.docstore_path = self.index_dir / "docstore.pkl"

        # in-memory
        self.docstore: Dict[str, Any] = {}
        self.index = None

        # load existing index/docstore if present
        self._load_index()

        print("[RAG] Initialized (Azure embeddings -> FAISS).")
        print(f"[RAG] Embedding deployment: {self.azure_embedding_deployment}")
        print(f"[RAG] Index path: {self.index_path}")

    # ---------------------------
    # FAISS index helpers
    # ---------------------------
    def _create_faiss_index(self):
        return faiss.IndexFlatIP(self.embedding_dim)

    def _load_index(self):
        if self.index_path.exists():
            try:
                self.index = faiss.read_index(str(self.index_path))
            except Exception as e:
                print("[RAG] Failed to read faiss index:", e)
                self.index = None

        if self.docstore_path.exists():
            try:
                with open(self.docstore_path, "rb") as f:
                    self.docstore = pickle.load(f)
            except Exception as e:
                print("[RAG] Failed to load docstore:", e)
                self.docstore = {}

        if self.index is None:
            self.index = self._create_faiss_index()

    def _save_index(self):
        faiss.write_index(self.index, str(self.index_path))
        with open(self.docstore_path, "wb") as f:
            pickle.dump(self.docstore, f)

    # ---------------------------
    # HTML extraction & cleaning
    # ---------------------------
    def _extract_text_from_html(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.extract()
        text = soup.get_text("\n")
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()

    def _clean(self, t: str) -> str:
        t = t.replace("\r", " ")
        t = re.sub(r"[ \t]+", " ", t)
        return t.strip()

    # ---------------------------
    # Chunking (word-based overlap)
    # ---------------------------
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

        # Overlap
        final = []
        for idx, c in enumerate(chunks):
            if idx == 0:
                final.append(c)
            else:
                prev = chunks[idx - 1]
                prev_words = prev.split()
                overlap_words = prev_words[-self.chunk_overlap:]
                overlap = " ".join(overlap_words)
                final.append((overlap + " " + c).strip())

        return [self._clean(x) for x in final if x.strip()]

    # ---------------------------
    # Embedding (batched + retry/backoff)
    # ---------------------------
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """Embed texts in batches with retry/backoff for Azure rate limits."""
        all_embeddings: List[List[float]] = []

        total = len(texts)
        if total == 0:
            return []

        for i in range(0, total, self.batch_size):
            batch = texts[i : i + self.batch_size]
            batch_index = i // self.batch_size + 1
            attempt = 0
            max_attempts = 6
            backoff = 60  # seconds (Azure recommends 60s after 429)
            while True:
                try:
                    print(f"[RAG] Embedding batch {batch_index} ({len(batch)} items)")
                    resp = self.client.embeddings.create(
                        model=self.azure_embedding_deployment,
                        input=batch,
                        extra_query={"api-version": self.api_version},
                    )
                    batch_embeds = [item.embedding for item in resp.data]
                    all_embeddings.extend(batch_embeds)
                    break
                except RateLimitError as e:
                    attempt += 1
                    wait = backoff * attempt
                    print(f"⚠️ Azure RateLimitError. attempt {attempt}/{max_attempts}. Sleeping {wait}s...")
                    time.sleep(wait)
                    if attempt >= max_attempts:
                        raise
                    continue
                except Exception as e:
                    # On network/timeout/other errors, do incremental backoff a few times then raise
                    attempt += 1
                    wait = 5 * attempt
                    print(f"⚠️ Embedding error (attempt {attempt}/{max_attempts}): {e}. Sleeping {wait}s...")
                    time.sleep(wait)
                    if attempt >= max_attempts:
                        raise
                    continue

        return all_embeddings

    # ---------------------------
    # Ingestion pipeline (file-level splitting for overly large files)
    # ---------------------------
    def ingest_files(self, paths: List[str]):
        """
        paths: list of file paths to ingest.
        This function will:
         - chunk each file
         - if a file yields > max_chunks_per_file, split it into multiple virtual parts
         - embed in batches
         - normalize and store in FAISS, updating docstore mapping
        """
        all_chunks: List[Chunk] = []

        for p in paths:
            p = Path(p)
            if not p.exists():
                continue

            if p.suffix.lower() in (".html", ".htm"):
                raw = p.read_text("utf-8", errors="ignore")
                text = self._extract_text_from_html(raw)
            else:
                text = p.read_text("utf-8", errors="ignore")

            text = self._clean(text)
            parts = self._chunk_text(text)

            if not parts:
                continue

            # If parts exceed max_chunks_per_file we split into multiple virtual parts
            if len(parts) > self.max_chunks_per_file:
                print(f"[RAG] File {p.name} produced {len(parts)} chunks; splitting into parts of {self.max_chunks_per_file}")
                # split into slices of max_chunks_per_file
                for part_idx in range(0, len(parts), self.max_chunks_per_file):
                    sub = parts[part_idx : part_idx + self.max_chunks_per_file]
                    for i, c in enumerate(sub):
                        all_chunks.append(Chunk(
                            id=f"{p.name}::part{part_idx//self.max_chunks_per_file}::{i}",
                            text=c,
                            source=str(p),
                            meta={"file": p.name, "chunk": part_idx + i, "part": part_idx//self.max_chunks_per_file}
                        ))
            else:
                for i, c in enumerate(parts):
                    all_chunks.append(Chunk(
                        id=f"{p.name}::{i}",
                        text=c,
                        source=str(p),
                        meta={"file": p.name, "chunk": i},
                    ))

        if not all_chunks:
            print("[RAG] No chunks found")
            return

        # Embed all chunks (batched with retry)
        print(f"[RAG] Embedding {len(all_chunks)} chunks…")
        texts = [c.text for c in all_chunks]
        embeddings = self.embed_texts(texts)

        # Normalize for cosine similarity (inner product)
        vecs = np.asarray(embeddings, dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1e-9
        vecs = vecs / norms

        # Map FAISS row index -> docstore
        base = self.index.ntotal
        for i, c in enumerate(all_chunks):
            key = str(base + i)
            self.docstore[key] = {"text": c.text, "meta": c.meta}

        # add to faiss
        self.index.add(vecs)
        self._save_index()

        print(f"[RAG] Ingested {len(all_chunks)} chunks. Total chunks = {len(self.docstore)}")

    # ---------------------------
    # Search
    # ---------------------------
    def search(self, query: str, k: int = 5):
        q = self._clean(query)
        emb = self.embed_texts([q])[0]
        v = np.array(emb, dtype=np.float32)
        v = v / (np.linalg.norm(v) + 1e-9)
        v = v.reshape(1, -1)

        if self.index.ntotal == 0:
            return []

        D, I = self.index.search(v, k)
        results = []
        for score, idx in zip(D[0], I[0]):
            item = self.docstore.get(str(idx))
            if not item:
                continue
            results.append({
                "score": float(score),
                "text": item["text"],
                "meta": item["meta"],
            })

        return results

    # -------------------------------------------
    # REMOVE A FILE'S EMBEDDINGS
    # -------------------------------------------
    def remove_file(self, filename: str):
        filename = filename.strip().lower()

        # Find all docstore keys belonging to this file
        keys_to_delete = [
            key for key, val in self.docstore.items()
            if val.get("meta", {}).get("file", "").lower() == filename
        ]

        if not keys_to_delete:
            print(f"[RAG] No chunks found for file: {filename}")
            return False

        print(f"[RAG] Removing {len(keys_to_delete)} chunks for file: {filename}")

        # Remove from docstore
        for key in keys_to_delete:
            del self.docstore[key]

        # Rebuild FAISS
        self._rebuild_faiss_index()
        self._save_index()

        return True

    # -------------------------------------------
    # REBUILD FAISS INDEX FROM DOCSTORE
    # -------------------------------------------
    def _rebuild_faiss_index(self):
        print("[RAG] Rebuilding FAISS index...")

        new_index = self._create_faiss_index()
        texts = []
        metas = []

        for key, entry in self.docstore.items():
            texts.append(entry["text"])
            metas.append(entry["meta"])

        if not texts:
            print("[RAG] No chunks left. Fresh FAISS index created.")
            self.index = new_index
            return

        embeddings = self.embed_texts(texts)

        vecs = np.asarray(embeddings, dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1e-9
        vecs = vecs / norms

        new_index.add(vecs)
        self.index = new_index
        print("[RAG] Rebuild complete.")
