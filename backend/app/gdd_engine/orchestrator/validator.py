import json
import jsonschema
import re


def clean_json_string(s: str) -> str:
    """
    Removes markdown code fences such as:
      ```json
      {...}
      ```
    Returns pure JSON string.
    """
    s = s.strip()

    # Remove ```json fences
    fence_pattern = r"^```(?:json)?\s*([\s\S]+?)\s*```$"
    match = re.match(fence_pattern, s, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()

    return s


def safe_extract_markdown(json_str: str):
    """
    Extracts {"markdown": "..."} even if JSON is malformed.
    This is required for Integration Agent which returns a huge Markdown string.
    """
    # Try normal JSON parse
    try:
        return json.loads(json_str)
    except:
        pass

    # Manual fallback: extract markdown field raw
    md_match = re.search(r'"markdown"\s*:\s*"([\s\S]*)"', json_str)
    if md_match:
        raw_md = md_match.group(1)

        # Unescape sequences
        raw_md = raw_md.replace('\\"', '"')
        raw_md = raw_md.replace("\\'", "'")
        raw_md = raw_md.replace("\\n", "\n")

        return {"markdown": raw_md}

    raise ValueError("Unable to extract markdown from integration output.")


def validate_json(output_str: str, schema_file: str):
    """
    Validates JSON output from any persona.
    - Removes markdown code fences
    - Handles Integration markdown special-case
    - Loads schema with UTF-8 (critical for Windows)
    - Returns (True, data) or (False, error_message)
    """

    print("\n=== VALIDATING JSON ===")
    print("Output str:", repr(output_str[:200]))
    print("Using schema file:", schema_file)

    cleaned = clean_json_string(output_str.strip())

    # SPECIAL CASE: Integration Agent (contains "markdown":)
    if cleaned.startswith("{") and '"markdown"' in cleaned:
        try:
            data = safe_extract_markdown(cleaned)
            return True, data
        except Exception as e:
            return False, f"Markdown extract failed: {e}"

    # NORMAL JSON VALIDATION PATH
    try:
        # JSON must parse cleanly
        data = json.loads(cleaned)

    except Exception as e:
        print("JSON PARSE FAILED:", e)
        return False, f"JSON parse error: {e}"

    # Load schema with UTF-8 (fixes Windows charmap bug)
    try:
        with open(schema_file, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except Exception as e:
        return False, f"Schema load error: {e}"

    # Validate
    try:
        jsonschema.validate(data, schema)
        return True, data
    except jsonschema.ValidationError as e:
        print("SCHEMA VALIDATION FAILED:", e)
        return False, f"Schema validation error: {e.message}"
