import os
from pathlib import Path
from dotenv import load_dotenv

# ----------------------------------------------------------
# 1) LOAD .env FILE EXPLICITLY (Windows-safe)
# ----------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
ENV_PATH = BASE_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
    print(f"🔄 Loaded .env from: {ENV_PATH}")
else:
    print(f"⚠️ WARNING: .env not found at: {ENV_PATH}")


# ----------------------------------------------------------
# 2) IMPORT AZURE LIBS
# ----------------------------------------------------------
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient


# ----------------------------------------------------------
# 3) TOGGLE: USE KEYVAULT OR .ENV
# ----------------------------------------------------------
USE_KEYVAULT = os.getenv("USE_KEYVAULT", "true").lower() == "true"
KEYVAULT_NAME = os.getenv("KEYVAULT_NAME")


def load_from_keyvault():
    """
    Fetch secrets from Azure Key Vault using DefaultAzureCredential.
    Works ONLY if AZURE_CLIENT_ID / SECRET / TENANT_ID are provided in .env.
    """

    if not KEYVAULT_NAME:
        raise RuntimeError("❌ KEYVAULT_NAME not set in environment variables.")

    kv_url = f"https://{KEYVAULT_NAME}.vault.azure.net/"
    print(f"🔐 Connecting to Key Vault: {kv_url}")

    # Force DefaultAzureCredential to use ClientSecretCredential (local dev)
    credential = DefaultAzureCredential(
        exclude_managed_identity_credential=True,
        exclude_powershell_credential=True
    )

    client = SecretClient(vault_url=kv_url, credential=credential)

    print("🔐 Fetching secrets from Azure Key Vault...")

    try:
        return {
            "AZURE_SPEECH_KEY": client.get_secret("azure-speech-key").value,
            "AZURE_SPEECH_REGION": client.get_secret("azure-speech-region").value,
            "AZURE_OPENAI_API_KEY": client.get_secret("azure-openai-api-key").value,
            "AZURE_OPENAI_ENDPOINT": client.get_secret("azure-openai-endpoint").value,
            "AZURE_OPENAI_DEPLOYMENT": client.get_secret("azure-openai-deployment").value,
        }
    except Exception as e:
        raise RuntimeError(f"❌ Failed to load secrets from Key Vault: {e}")


def load_from_env():
    """Fallback loader for local .env usage."""

    print("📄 Loading secrets from local .env file...")

    return {
        "AZURE_SPEECH_KEY": os.getenv("AZURE_SPEECH_KEY"),
        "AZURE_SPEECH_REGION": os.getenv("AZURE_SPEECH_REGION"),
        "AZURE_OPENAI_API_KEY": os.getenv("AZURE_OPENAI_API_KEY"),
        "AZURE_OPENAI_ENDPOINT": os.getenv("AZURE_OPENAI_ENDPOINT"),
        "AZURE_OPENAI_DEPLOYMENT": os.getenv("AZURE_OPENAI_DEPLOYMENT"),
    }


# ----------------------------------------------------------
# 4) MAIN CONFIG LOADER
# ----------------------------------------------------------
def load_config():
    if USE_KEYVAULT:
        print("🔧 USE_KEYVAULT = true → loading from Azure Key Vault")
        return load_from_keyvault()
    else:
        print("🔧 USE_KEYVAULT = false → loading from .env")
        return load_from_env()


CONFIG = load_config()

print("✅ Config loaded successfully:")
print({
    "speech_region": CONFIG.get("AZURE_SPEECH_REGION"),
    "openai_endpoint": CONFIG.get("AZURE_OPENAI_ENDPOINT"),
    "deployment": CONFIG.get("AZURE_OPENAI_DEPLOYMENT"),
})
