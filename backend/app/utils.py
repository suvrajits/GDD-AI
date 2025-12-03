# backend/app/utils.py
from openai import OpenAI
from app.config import CONFIG

API_KEY    = CONFIG["AZURE_OPENAI_API_KEY"]
ENDPOINT   = CONFIG["AZURE_OPENAI_ENDPOINT"].rstrip("/")
DEPLOYMENT = CONFIG["AZURE_OPENAI_CHAT_DEPLOYMENT"]
API_VERSION = "2024-08-01-preview"

client = OpenAI(
    api_key=API_KEY,
    base_url=f"{ENDPOINT}/openai/deployments/{DEPLOYMENT}",
    default_headers={"api-key": API_KEY}
)


async def call_openai_chat(messages, temperature=0.7):
    """Simple helper: agents call this to get a response."""
    resp = client.chat.completions.create(
        model=DEPLOYMENT,
        messages=messages,
        temperature=temperature,
        extra_query={"api-version": API_VERSION}
    )
    return resp.choices[0].message["content"]
