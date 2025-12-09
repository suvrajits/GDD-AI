# llm_orchestrator.py
import asyncio
from openai import OpenAI
from .config import CONFIG
from app.routes.rag_routes import rag

API_KEY = CONFIG.get("AZURE_OPENAI_API_KEY")
ENDPOINT = CONFIG.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
DEPLOYMENT = CONFIG.get("AZURE_OPENAI_DEPLOYMENT")
API_VERSION = "2024-08-01-preview"

client = OpenAI(
    api_key=API_KEY,
    base_url=f"{ENDPOINT}/openai/deployments/{DEPLOYMENT}",
    default_headers={"api-key": API_KEY}
)

async def stream_llm(user_text: str):
    """
    Asynchronously stream LLM token deltas (yields strings).
    Performs a RAG search first (best-effort).
    """
    print("🔥 LLM CALL ->", user_text)

    # 1) RAG context (best-effort)
    context_text = ""
    try:
        rag_results = rag.search(user_text, k=5)
        if rag_results:
            context_text = "\n\n".join(
                f"[Source: {r['meta'].get('file','unknown')}]\n{r['text']}"
                for r in rag_results
            )
    except Exception as e:
        print("RAG search error:", e)
        context_text = ""

    if context_text:
        system_context = (
            "You are a top 1% hybrid-casual game designer.\n"
            "Use the following embedded knowledge to answer accurately.\n\n"
            "=== START RAG CONTEXT ===\n"
            f"{context_text}\n"
            "=== END RAG CONTEXT ===\n"
        )
    else:
        system_context = (
            "You are a top 1% hybrid-casual game designer. "
            "No RAG context available for this question."
        )

    try:
        # Use streaming completions from Azure OpenAI-compatible SDK
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
            # chunk may be an object with .choices, similar to openai streaming
            choices = getattr(chunk, "choices", None)
            if not choices:
                await asyncio.sleep(0)
                continue

            delta = choices[0].delta
            if delta and getattr(delta, "content", None):
                yield delta.content

            # tiny cooperative sleep so event loop can run
            await asyncio.sleep(0)

    except Exception as e:
        err = f"[LLM ERROR] {e}"
        print("❌ LLM Streaming Error:", err)
        yield err

async def run_completion(prompt: str, max_tokens: int = 150):
    """
    Simple one-shot non-streaming completion for internal system use.
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI()

    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.4,
    )

    return resp.choices[0].message["content"]
