# backend/app/llm_orchestrator.py

import asyncio
from openai import AzureOpenAI
from .config import CONFIG

API_KEY = CONFIG.get("AZURE_OPENAI_API_KEY")
ENDPOINT = CONFIG.get("AZURE_OPENAI_ENDPOINT")
DEPLOYMENT = CONFIG.get("AZURE_OPENAI_DEPLOYMENT")

if not API_KEY or not ENDPOINT or not DEPLOYMENT:
    raise RuntimeError("Azure OpenAI credentials missing!")

# Azure OpenAI Client
client = AzureOpenAI(
    api_key=API_KEY,
    azure_endpoint=ENDPOINT,
    api_version="2024-08-01-preview"
)

# Main callable used in main.py
async def call_llm(user_text: str) -> str:
    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=DEPLOYMENT,
            messages=[
                {"role": "system", "content": "You are a top 1% game designer specializing in hybrid-casual games. Be sharp, candid, and sophisticated."},
                {"role": "user", "content": user_text}
            ]
        )

        return response.choices[0].message.content

    except Exception as e:
        return f"[LLM ERROR] {e}"
