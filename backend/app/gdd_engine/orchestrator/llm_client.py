import os
import sys
import json
from azure.core.credentials import AzureKeyCredential
from openai import AzureOpenAI


# ============================================================
# 🔧 FIX: Add backend/app to sys.path so we can import config.py
# ============================================================

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))              # .../gdd_engine/orchestrator
GDD_ENGINE_DIR = os.path.dirname(CURRENT_DIR)                         # .../gdd_engine
APP_DIR = os.path.dirname(GDD_ENGINE_DIR)                             # .../app

if APP_DIR not in sys.path:
    sys.path.append(APP_DIR)

from config import CONFIG   # now this import works reliably


# ============================================================
# 🔐 Azure OpenAI Setup
# ============================================================

AZURE_OPENAI_KEY = CONFIG.get("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = CONFIG.get("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_CHAT_DEPLOYMENT = CONFIG.get("AZURE_OPENAI_CHAT_DEPLOYMENT")

if not AZURE_OPENAI_KEY or not AZURE_OPENAI_ENDPOINT:
    raise RuntimeError("❌ Azure OpenAI credentials missing from CONFIG")

client = AzureOpenAI(
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_key=AZURE_OPENAI_KEY,
    api_version="2024-08-01-preview"
)


def call_llm(system_prompt: str, user_prompt: str) -> str:

    print("\n=== CALLING AZURE OPENAI ===")
    print("System:", system_prompt[:200], "...")
    print("User:", user_prompt[:200], "...")

    response = client.chat.completions.create(
        model=AZURE_OPENAI_CHAT_DEPLOYMENT,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.2
    )

    print("\n=== RAW FULL RESPONSE OBJECT TYPE ===")
    print(type(response))

    print("\n=== RAW CHOICE TYPE ===")
    print(type(response.choices[0]))

    print("\n=== RAW MESSAGE TYPE ===")
    print(type(response.choices[0].message))

    print("\n=== RAW MESSAGE.CONTENT TYPE ===")
    print(type(response.choices[0].message.content))

    print("\n=== RAW MESSAGE.CONTENT VALUE ===")
    print(response.choices[0].message.content)

    print("\n=== repr(MESSAGE.CONTENT) ===")
    print(repr(response.choices[0].message.content))

    # Temporarily return empty to avoid validation
    return response.choices[0].message.content
