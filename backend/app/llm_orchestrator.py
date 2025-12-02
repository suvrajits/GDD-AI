# backend/app/llm_orchestrator.py

import asyncio
from openai import OpenAI
from .config import CONFIG
from app.routes.rag_routes import rag
API_KEY    = CONFIG["AZURE_OPENAI_API_KEY"]
ENDPOINT   = CONFIG["AZURE_OPENAI_ENDPOINT"].rstrip("/")
DEPLOYMENT = CONFIG["AZURE_OPENAI_DEPLOYMENT"]
API_VERSION = "2024-08-01-preview"

client = OpenAI(
    api_key=API_KEY,
    base_url=f"{ENDPOINT}/openai/deployments/{DEPLOYMENT}",
    default_headers={"api-key": API_KEY}
)

async def stream_llm(user_text: str):
    print("🔥 LLM CALL ->", user_text)

    try:
        stream = client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a top 1% hybrid-casual game designer. "
                        "Sharp, candid, sophisticated."
                    )
                },
                {"role": "user", "content": user_text}
            ],
            stream=True,
            extra_query={"api-version": API_VERSION}
        )

        for chunk in stream:
            # Azure sometimes sends empty chunks
            choices = chunk.choices
            if not choices:
                continue

            delta = choices[0].delta
            if delta and getattr(delta, "content", None):
                yield delta.content

            await asyncio.sleep(0)

    except Exception as e:
        err = f"[LLM ERROR] {e}"
        print("❌ LLM Streaming Error:", err)
        yield err

async def stream_llm(user_text: str):
    print("🔥 LLM CALL ->", user_text)

    # 1) RAG SEARCH FIRST
    rag_results = []
    try:
        rag_results = rag.search(user_text, k=5)
    except Exception as e:
        print("RAG search error:", e)

    context_text = "\n\n".join(
        f"[Source: {r['meta']['file']}]\n{r['text']}"
        for r in rag_results
    )

    if context_text:
        system_context = (
            "You are a top 1% hybrid-casual game designer.\n"
            "Use the following embedded knowledge to answer accurately.\n\n"
            f"=== START RAG CONTEXT ===\n"
            f"{context_text}\n"
            f"=== END RAG CONTEXT ===\n"
        )
    else:
        system_context = (
            "You are a top 1% hybrid-casual game designer. "
            "No RAG context available for this question."
        )

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
        yield f"[LLM ERROR] {e}"
