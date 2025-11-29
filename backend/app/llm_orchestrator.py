# backend/app/llm_orchestrator.py
import json
from openai import AsyncAzureOpenAI
from .config import CONFIG

# Pull values loaded from Key Vault
AZURE_OPENAI_API_KEY = CONFIG["AZURE_OPENAI_API_KEY"]
AZURE_OPENAI_ENDPOINT = CONFIG["AZURE_OPENAI_ENDPOINT"]
AZURE_OPENAI_DEPLOYMENT = CONFIG["AZURE_OPENAI_DEPLOYMENT"]

# Create Azure OpenAI client
client = AsyncAzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_version="2025-01-01-preview"
)

async def call_llm(user_text: str):
    """
    Sends the committed ASR text to Azure OpenAI and returns a clean response.
    """
    try:
        response = await client.chat.completions.create(
            model=AZURE_OPENAI_DEPLOYMENT,
            messages=[
                {"role": "system", "content": "You are a realtime summarizer. Reply concisely."},
                {"role": "user", "content": user_text}
            ]
        )

        # Azure returns:
        # response.choices[0].message.content
        return response.choices[0].message.content

    except Exception as e:
        print("LLM ERROR:", e)
        return "[LLM error]"
