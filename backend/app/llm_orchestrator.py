# backend/app/llm_orchestrator.py

import asyncio
from openai import OpenAI
from pathlib import Path
from .config import CONFIG
from app.routes.rag_routes import rag
from app.agents.meta_agent import generate_gdd

# --------------------------
# CONFIG
# --------------------------
API_KEY     = CONFIG["AZURE_OPENAI_API_KEY"]
ENDPOINT    = CONFIG["AZURE_OPENAI_ENDPOINT"].rstrip("/")
DEPLOYMENT  = CONFIG["AZURE_OPENAI_DEPLOYMENT"]
API_VERSION = "2024-08-01-preview"

client = OpenAI(
    api_key=API_KEY,
    base_url=f"{ENDPOINT}/openai/deployments/{DEPLOYMENT}",
    default_headers={"api-key": API_KEY}
)

# --------------------------
# NYRA Persona
# --------------------------
NYRA_PERSONA = Path("app/persona/nyra.txt").read_text(encoding="utf-8")


# Greeting detection
GREETINGS = {"hi", "hello", "hey", "yo", "sup", "hola", "namaste"}

# GDD trigger keywords
GDD_KEYWORDS = [
    "generate gdd", "create gdd", "full gdd",
    "game design document", "design document",
    "full design doc", "build the gdd"
]


# ---------------------------------------------------
# CHAT MODE STREAMING (NYRA PERSONALITY + RAG CONTEXT)
# ---------------------------------------------------
async def stream_llm(user_text: str, session_state=None):
    """
    Handles conversational chat with NYRA personality +
    RAG augmentation + optional GDD auto-trigger.
    """

    print("🔥 NYRA CHAT CALL ->", user_text)

    # 1) FIRST-TIME GREETING HANDLER
    if session_state is not None and session_state.get("first_message", True):
        lowered = user_text.lower().strip()
        if lowered in GREETINGS:
            session_state["first_message"] = False
            yield "Hi, I’m NYRA — your game design companion. Ready when you are."
            return

        session_state["first_message"] = False

    # 2) NATURAL LANGUAGE GDD TRIGGER
    text_l = user_text.lower()
    if any(k in text_l for k in GDD_KEYWORDS):
        yield "⏳ Starting full GDD generation pipeline...\n"

        try:
            result = await generate_gdd(user_text)
            if result["status"] == "ok":
                yield "🎉 GDD ready:\n"
                yield result["aggregated_gdd"]
            else:
                yield f"❌ GDD generation failed:\n{result}"
        except Exception as e:
            yield f"❌ Error during GDD generation: {e}"
        return

    # 3) RAG-AUGMENTED CHAT WITH NYRA
    rag_results = []
    try:
        rag_results = rag.search(user_text, k=5)
    except Exception as e:
        print("RAG search error:", e)

    rag_context = "\n\n".join(
        f"[Source: {r['meta']['file']}]\n{r['text']}"
        for r in rag_results
    )

    if rag_context:
        system_context = (
            f"{NYRA_PERSONA}\n\n"
            "Use the following embedded knowledge when helpful and relevant.\n"
            "=== START RAG CONTEXT ===\n"
            f"{rag_context}\n"
            "=== END RAG CONTEXT ==="
        )
    else:
        system_context = (
            f"{NYRA_PERSONA}\n\n"
            "No external RAG context available. Answer using your own expertise."
        )

    # 4) CALL LLM WITH STREAMING
    try:
        stream = client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[
                {"role": "system", "content": system_context},
                {"role": "user", "content": user_text}
            ],
            stream=True,
            extra_query={"api-version": API_VERSION}
        )

        for chunk in stream:
            choices = chunk.choices
            if not choices:
                continue

            delta = choices[0].delta
            if delta and getattr(delta, "content", None):
                yield delta.content

            await asyncio.sleep(0)

    except Exception as e:
        yield f"[NYRA ERROR] {e}"
