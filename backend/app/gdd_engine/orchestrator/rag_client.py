# backend/app/gdd_engine/orchestrator/rag_client.py

"""
RAG placeholder.
Your real implementation will query FAISS / Chroma / Azure Search.
For now, return small generic helpful snippets so the orchestrator demo looks richer.
"""

from typing import List

def retrieve(query: str, top_k: int = 8) -> List[str]:
    mock = [
        "Industry benchmark: top mobile strategy games emphasise clarity in early tutorials.",
        "Good GDDs always define their core loop before expanding meta-systems.",
        "User onboarding is the strongest predictor of D1 retention.",
        "Team-based mechanics must reinforce cooperation, not parallel play.",
        "Fusion systems risk complexity creep—keep UX extremely clear.",
    ]
    return mock[:top_k]
