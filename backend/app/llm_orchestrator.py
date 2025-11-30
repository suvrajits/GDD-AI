# backend/app/llm_orchestrator.py
import asyncio

async def call_llm(text: str) -> str:
    # simple echo for testing latency and flow
    await asyncio.sleep(0.1)
    return f"LLM-echo: {text}"
