import json
from orchestrator.persona_router import load_persona, load_prompt, load_schema
from orchestrator.llm_client import call_llm
from orchestrator.validator import validate_json


# ===============================================================
#  PERSONA → FILE MAPPING (MATCHES YOUR ACTUAL FOLDER CONTENTS)
# ===============================================================

PERSONA_MAP = {
    "director": {
        "persona": "game_director",
        "prompt": "director",
        "schema": "director"
    },
    "lead": {
        "persona": "lead_game_designer",
        "prompt": "lead",
        "schema": "lead"
    },
    "systems": {
        "persona": "systems_designer",
        "prompt": "systems",
        "schema": "systems"
    },
    "ux": {
        "persona": "ux_director",
        "prompt": "ux",
        "schema": "ux"
    },
    "pm": {
        "persona": "product_manager",
        "prompt": "pm",
        "schema": "pm"
    },
    "integration": {
        "persona": "integration_agent",
        "prompt": "integration",
        "schema": "integration"
    },
    "reviewer": {
        "persona": "reviewer_agent",
        "prompt": "reviewer",
        "schema": "reviewer"
    }
}


# ===============================================================
#  ORCHESTRATOR ENGINE
# ===============================================================

class GDDOrchestrator:

    def __init__(self, concept: str):
        self.concept = concept
        self.outputs = {}

    def run_persona(self, persona_name: str, extra_context=""):
        """
        Runs a persona based on the explicit file mapping above.
        """

        filenames = PERSONA_MAP[persona_name]

        persona_file = filenames["persona"]
        prompt_file = filenames["prompt"]
        schema_file = filenames["schema"]

        persona_card = load_persona(persona_file)
        persona_prompt = load_prompt(prompt_file)
        schema_path = load_schema(schema_file)

        # Build system and user prompts
        system_msg = (
            f"PERSONA_CARD:\n{json.dumps(persona_card, indent=2)}"
        )

        user_msg = (
            f"Concept: {self.concept}\n\n"
            f"Context:\n{extra_context}\n\n"
            f"{persona_prompt}"
        )

        raw_output = call_llm(system_msg, user_msg)

        # Validate JSON
        valid, result = validate_json(raw_output, schema_path)
        if not valid:
            raise Exception(f"[INVALID JSON OUTPUT from {persona_name}] → {result}")

        self.outputs[persona_name] = result
        return result

    def run_pipeline(self):
        director = self.run_persona("director")
        lead = self.run_persona("lead", json.dumps(director))
        systems = self.run_persona("systems", json.dumps({"director": director, "lead": lead}))
        ux = self.run_persona("ux", json.dumps({"director": director, "lead": lead}))
        pm = self.run_persona("pm", json.dumps({
            "director": director,
            "lead": lead,
            "systems": systems
        }))
        integration = self.run_persona("integration", json.dumps(self.outputs))
        reviewer = self.run_persona("reviewer", json.dumps(self.outputs))

        return {
            "director": director,
            "lead": lead,
            "systems": systems,
            "ux": ux,
            "pm": pm,
            "integration": integration,
            "reviewer": reviewer
        }
