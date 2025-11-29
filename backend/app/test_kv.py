from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

KV_NAME = "gdd-speech-kv"
KV_URI = f"https://{KV_NAME}.vault.azure.net/"

credential = DefaultAzureCredential()
client = SecretClient(vault_url=KV_URI, credential=credential)

print("Fetching secrets...")

speech_key = client.get_secret("azure-speech-key").value
print("Speech key:", speech_key[:6], "***")

openai_key = client.get_secret("azure-openai-api-key").value
print("OpenAI key:", openai_key[:6], "***")

print("SUCCESS: Backend can read Key Vault secrets!")
