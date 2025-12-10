import os
from azure.keyvault.secrets import SecretClient
from azure.identity import ClientSecretCredential
from dotenv import load_dotenv
load_dotenv()

CONFIG = {}

USE_KEYVAULT = os.getenv("USE_KEYVAULT", "false").lower() == "true"

if USE_KEYVAULT:
    kv_name = os.getenv("KEYVAULT_NAME")

    if not kv_name:
        raise RuntimeError("❌ KEYVAULT_NAME is missing in .env")

    url = f"https://{kv_name}.vault.azure.net/"
    print("🔐 Connecting to KeyVault:", url)

    # Proper KV auth
    credential = ClientSecretCredential(
        tenant_id=os.getenv("AZURE_TENANT_ID"),
        client_id=os.getenv("AZURE_CLIENT_ID"),
        client_secret=os.getenv("AZURE_CLIENT_SECRET")
    )
    print("🔑 ClientSecretCredential created OK")

    client = SecretClient(vault_url=url, credential=credential)

    secrets = {}

    try:
        print("📥 Fetching list of secrets...")
        props = list(client.list_properties_of_secrets())
        print("FOUND SECRETS:", [p.name for p in props])

        for p in props:
            secrets[p.name] = client.get_secret(p.name).value

    except Exception as e:
        print("❌ ERROR while listing secrets:", e)

    print("📦 SECRETS LOADED:", secrets)

    CONFIG["AZURE_SPEECH_KEY"]        = secrets.get("azure-speech-key")
    CONFIG["AZURE_SPEECH_REGION"]     = secrets.get("azure-speech-region")
    CONFIG["AZURE_OPENAI_API_KEY"]    = secrets.get("azure-openai-api-key")
    CONFIG["AZURE_OPENAI_ENDPOINT"]   = secrets.get("azure-openai-endpoint")
    CONFIG["AZURE_OPENAI_DEPLOYMENT"] = secrets.get("azure-openai-deployment")

else:
    CONFIG["AZURE_SPEECH_KEY"]        = os.getenv("AZURE_SPEECH_KEY")
    CONFIG["AZURE_SPEECH_REGION"]     = os.getenv("AZURE_SPEECH_REGION")
    CONFIG["AZURE_OPENAI_API_KEY"]     = os.getenv("AZURE_OPENAI_API_KEY")
    CONFIG["AZURE_OPENAI_ENDPOINT"]    = os.getenv("AZURE_OPENAI_ENDPOINT")
    CONFIG["AZURE_OPENAI_DEPLOYMENT"]  = os.getenv("AZURE_OPENAI_DEPLOYMENT")

# ✅ REQUIRED FIX — Normalize endpoint
if CONFIG.get("AZURE_OPENAI_ENDPOINT"):
    CONFIG["AZURE_OPENAI_ENDPOINT"] = CONFIG["AZURE_OPENAI_ENDPOINT"].rstrip("/")


CONFIG["AZURE_OPENAI_CHAT_DEPLOYMENT"] = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT")
CONFIG["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"] = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")

print("CONFIG LOADED:", CONFIG)
