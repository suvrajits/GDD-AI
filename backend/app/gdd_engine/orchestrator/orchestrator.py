"""
orchestrator.py
Improved GDD orchestrator:
- Passes `answers` + `kb_snippets` to persona runs
- Uses persona prompts / personas / schemas from persona_router
- Validates JSON outputs
- Runs final integration persona to produce `integration_markdown`
- Exposes refine_section() helper
"""

import json
import logging
from typing import Dict, Any, Optional

from .persona_router import load_persona, load_prompt, load_schema
from .llm_client import call_llm
from .validator import validate_json

# Optional RAG client import. If not present, we'll gracefully continue.
try:
    from .rag_client import retrieve as rag_retrieve
except Exception:
    rag_retrieve = None  # type: ignore

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# PERSONA MAP must match the prompt / persona / schema filenames in your repo
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


class GDDOrchestrator:
    """
    Orchestrator coordinates persona runs and assembles the final GDD.
    """

    def __init__(self, concept: str, answers: Optional[Dict[str, Any]] = None, use_rag: bool = True):
        """
        :param concept: short concept/title of the game
        :param answers: dict of user answers collected by the GDD wizard (optional)
        :param use_rag: whether to attempt RAG retrieval (falls back if rag_client missing)
        """
        self.concept = concept
        self.answers = answers or {}
        self.use_rag = use_rag and (rag_retrieve is not None)
        self.kb_snippets = []
        self.outputs: Dict[str, Any] = {}

    # ---------------------------
    # Helper: RAG retrieval
    # ---------------------------
    def fetch_rag_context(self):
        """
        If RAG is available, query it with the concept + answers to fetch relevant context.
        Stores results in self.kb_snippets (list of short text snippets).
        """
        if not self.use_rag:
            logger.info("RAG disabled or rag_client not found; skipping retrieval.")
            return []

        try:
            query_text = self.concept + "\n\n" + json.dumps(self.answers, ensure_ascii=False)
            snippets = rag_retrieve(query_text, top_k=8)  # rag_retrieve should return list[str]
            if isinstance(snippets, list):
                self.kb_snippets = snippets
            else:
                # Support other return shapes
                self.kb_snippets = list(snippets)
            logger.info("RAG: retrieved %d snippets", len(self.kb_snippets))
            return self.kb_snippets
        except Exception as e:
            logger.exception("RAG retrieval failed: %s", e)
            self.kb_snippets = []
            return []

    # ---------------------------
    # Core persona runner
    # ---------------------------
    def run_persona(self, persona_name: str, extra_context: str = "") -> Any:
        """
        Run a persona using PERSONA_MAP configuration.
        :param persona_name: key in PERSONA_MAP e.g., 'director', 'lead'
        :param extra_context: optional JSON string or free text passed to the prompt
        :return: validated JSON-like dict output (as returned by validate_json)
        """
        if persona_name not in PERSONA_MAP:
            raise ValueError(f"Unknown persona '{persona_name}'")

        filenames = PERSONA_MAP[persona_name]
        persona_file = filenames["persona"]
        prompt_file = filenames["prompt"]
        schema_file = filenames["schema"]

        persona_card = load_persona(persona_file)
        persona_prompt_text = load_prompt(prompt_file)
        schema_path = load_schema(schema_file)

        system_msg = f"PERSONA_CARD:\n{json.dumps(persona_card, indent=2)}"

        # Build the user message. Include concept, answers, kb_snippets, plus any extra context
        user_payload = {
            "concept": self.concept,
            "answers": self.answers,
            "kb_snippets": self.kb_snippets,
            "extra": extra_context
        }

        user_msg = (
            f"Context:\n{json.dumps(user_payload, ensure_ascii=False, indent=2)}\n\n"
            f"{persona_prompt_text}"
        )

        logger.info("Calling LLM for persona '%s'...", persona_name)
        raw_output = call_llm(system_msg, user_msg)

        # Validate JSON output using the persona schema
        valid, result = validate_json(raw_output, schema_path)
        if not valid:
            # include debug info and raise to bubble up to caller
            logger.error("Invalid JSON output from persona '%s': %s", persona_name, result)
            raise Exception(f"[INVALID JSON OUTPUT from {persona_name}] → {result}")

        # Store & return
        self.outputs[persona_name] = result
        logger.info("Persona '%s' completed successfully.", persona_name)
        return result

    # ---------------------------
    # Pipeline run
    # ---------------------------
    def run_pipeline(self) -> Dict[str, Any]:
        """
        Execute the full multi-persona pipeline in intended order and then run integration persona.
        Returns dict with all persona outputs & integration_markdown.
        """
        # 1) fetch RAG context (optional)
        self.fetch_rag_context()

        # 2) run director
        director = self.run_persona("director")

        # 3) run lead with director context
        lead = self.run_persona("lead", extra_context=json.dumps({"director": director}, ensure_ascii=False))

        # 4) systems (pass director + lead)
        systems = self.run_persona("systems", extra_context=json.dumps({"director": director, "lead": lead}, ensure_ascii=False))

        # 5) ux
        ux = self.run_persona("ux", extra_context=json.dumps({"director": director, "lead": lead, "systems": systems}, ensure_ascii=False))

        # 6) pm (pass director, lead, systems, ux)
        pm = self.run_persona("pm", extra_context=json.dumps({"director": director, "lead": lead, "systems": systems, "ux": ux}, ensure_ascii=False))

        # 7) run integration agent (pass everything + user answers + kb)
        integration_input = {
            "director": director,
            "lead": lead,
            "systems": systems,
            "ux": ux,
            "pm": pm,
            "answers": self.answers,
            "kb_snippets": self.kb_snippets
        }

        integration = self.run_persona("integration", extra_context=json.dumps(integration_input, ensure_ascii=False))

        # 8) optional reviewer
        try:
            reviewer = self.run_persona("reviewer", extra_context=json.dumps({
                "director": director,
                "lead": lead,
                "systems": systems,
                "ux": ux,
                "pm": pm,
                "integration": integration
            }, ensure_ascii=False))
        except Exception as e:
            logger.warning("Reviewer persona failed: %s", e)
            reviewer = {"warning": "reviewer failed", "error": str(e)}

        # Save aggregated output
        final = {
            "director": director,
            "lead": lead,
            "systems": systems,
            "ux": ux,
            "pm": pm,
            "integration": integration,
            "reviewer": reviewer
        }

        self.outputs = final
        return final

    # ---------------------------
    # Utility: refine a specific section
    # ---------------------------
    def refine_section(self, section_persona: str, notes: str, base_context: Optional[Dict[str, Any]] = None) -> Any:
        """
        Run a single persona to refine a section of the GDD.
        :param section_persona: persona key (e.g., 'systems' or 'ux' or 'lead')
        :param notes: user instruction e.g. "expand combat damage formula, add examples"
        :param base_context: optional base context to pass (e.g., current integration result)
        """
        extra = {
            "notes": notes,
            "base_context": base_context or {},
            "answers": self.answers,
            "kb_snippets": self.kb_snippets
        }
        return self.run_persona(section_persona, extra_context=json.dumps(extra, ensure_ascii=False))

    # ---------------------------
    # Small helper: quick orchestration entry (static)
    # ---------------------------
    @classmethod
    def orchestrate(cls, concept: str, answers: Optional[Dict[str, Any]] = None, use_rag: bool = True) -> Dict[str, Any]:
        """
        Convenience method used by your API handler.
        """
        orchestrator = cls(concept, answers=answers, use_rag=use_rag)
        return orchestrator.run_pipeline()


# If invoked directly for quick manual testing:
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--concept", required=True)
    parser.add_argument("--answers", default="{}")
    args = parser.parse_args()

    answers = json.loads(args.answers)
    orch = GDDOrchestrator(args.concept, answers=answers, use_rag=False)
    result = orch.run_pipeline()
    print(json.dumps(result.keys(), indent=2))

