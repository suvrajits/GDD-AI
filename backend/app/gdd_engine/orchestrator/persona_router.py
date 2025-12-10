import json
import os

BASE_PATH = os.path.dirname(os.path.dirname(__file__))
PERSONA_DIR = os.path.join(BASE_PATH, "personas")
PROMPT_DIR = os.path.join(BASE_PATH, "prompts")
SCHEMA_DIR = os.path.join(BASE_PATH, "schemas")


def load_persona(name: str) -> dict:
    """
    Load persona JSON from personas/<name>.json
    """
    path = os.path.join(PERSONA_DIR, f"{name}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_prompt(name: str) -> str:
    """
    Load prompt text from prompts/<name>_prompt.txt
    """
    path = os.path.join(PROMPT_DIR, f"{name}_prompt.txt")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_schema(name: str) -> str:
    """
    Return absolute path to schemas/<name>_schema.json
    """
    return os.path.join(SCHEMA_DIR, f"{name}_schema.json")
