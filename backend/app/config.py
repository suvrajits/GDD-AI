import os
from azure.keyvault.secrets import SecretClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
load_dotenv()


CONFIG = {}

USE_KEYVAULT = os.getenv("USE_KEYVAULT", "false").lower() == "true"

print("USE_KEYVAULT =", USE_KEYVAULT)

if USE_KEYVAULT:
    kv_name = os.getenv("KEYVAULT_NAME")
    print("ENV KEYVAULT_NAME =", kv_name)

    if not kv_name:
        raise RuntimeError("❌ KEYVAULT_NAME is missing in .env")

    url = f"https://{kv_name}.vault.azure.net/"
    print("🔐 Connecting to KeyVault:", url)

    try:
        credential = DefaultAzureCredential()
        print("🔑 DefaultAzureCredential created OK")
    except Exception as e:
        print("❌ DefaultAzureCredential init FAILED:", e)

    client = SecretClient(vault_url=url, credential=credential)

    secrets = {}

    try:
        print("📥 Fetching list of secrets...")
        props = list(client.list_properties_of_secrets())
        print("FOUND SECRETS:", [p.name for p in props])

        for p in props:
            val = client.get_secret(p.name).value
            secrets[p.name] = val

    except Exception as e:
        print("❌ ERROR while listing secrets:", e)

    print("📦 SECRETS LOADED:", secrets)

    # Map exactly by the names in your KeyVault
    CONFIG["AZURE_SPEECH_KEY"]    = secrets.get("azure-speech-key")
    CONFIG["AZURE_SPEECH_REGION"] = secrets.get("azure-speech-region")

else:
    CONFIG["AZURE_SPEECH_KEY"] = os.getenv("AZURE_SPEECH_KEY")
    CONFIG["AZURE_SPEECH_REGION"] = os.getenv("AZURE_SPEECH_REGION")

print("CONFIG LOADED:", CONFIG)
