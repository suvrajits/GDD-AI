# app/routes/gdd_routes.py
import asyncio
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from app.agents.meta_agent import generate_gdd

router = APIRouter()

# ----------------------------
# Stream helper
# ----------------------------
async def stream_gdd_pipeline(user_prompt: str):
    """
    Generator that streams progress to the frontend as Server-Sent Events (SSE).
    """
    # Step 1: Start
    yield "event: message\ndata: Starting GDD pipeline...\n\n"
    await asyncio.sleep(0.1)

    # Step 2: run full pipeline
    yield "event: message\ndata: Running multi-agent orchestrator...\n\n"

    try:
        result = await generate_gdd(user_prompt)
        yield f"event: done\ndata: {result['aggregated_gdd']}\n\n"
    except Exception as e:
        yield f"event: error\ndata: {str(e)}\n\n"


# ----------------------------
# POST /gdd/generate — STREAMING
# ----------------------------
@router.post("/gdd/generate")
async def gdd_generate(request: Request):
    """
    Stream GDD generation step-by-step using SSE.
    """
    body = await request.json()
    user_prompt = body.get("prompt", "")

    if not user_prompt:
        return {"error": "Missing 'prompt'."}

    return StreamingResponse(
        stream_gdd_pipeline(user_prompt),
        media_type="text/event-stream"
    )
