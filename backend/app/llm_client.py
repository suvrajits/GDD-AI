# backend/app/llm_client.py
from openai import OpenAI
from app.config import CONFIG

API_KEY = CONFIG["AZURE_OPENAI_API_KEY"]
ENDPOINT = CONFIG["AZURE_OPENAI_ENDPOINT"].rstrip("/")
DEPLOYMENT = CONFIG["AZURE_OPENAI_CHAT_DEPLOYMENT"]

client = OpenAI(
    api_key=API_KEY,
    base_url=f"{ENDPOINT}/openai/deployments/{DEPLOYMENT}",
    default_headers={"api-key": API_KEY}
)

def call_openai_chat(messages: list, temperature: float = 0.7):
    resp = client.chat.completions.create(
        model=DEPLOYMENT,
        messages=messages,
        temperature=temperature,
        extra_query={"api-version": "2024-08-01-preview"}
    )
    return resp.choices[0].message["content"]
