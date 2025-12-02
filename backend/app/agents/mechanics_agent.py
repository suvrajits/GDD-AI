# app/agents/mechanics_agent.py

from llm_orchestrator import call_openai_chat


class MechanicsAgent:
    """Responsible for core loop, systems, controls, game feel, and progression."""

    SYSTEM_PROMPT = """
    You are a top-tier Game Systems Designer.
    Use MDA, Flow Theory, and proven hypercasual/hybrid frameworks.

    Deliverables:
    - Core Loop (3–5 steps)
    - Meta Loop
    - Control Scheme
    - Systems Overview
    - Progression
    - Difficulty Curves
    """

    @staticmethod
    async def generate_mechanics(game_concept, retrieved_knowledge=None):
        prompt = f"""
        Game Concept:
        {game_concept}

        Retrieved Knowledge:
        {retrieved_knowledge}
        """
        return await call_openai_chat(MechanicsAgent.SYSTEM_PROMPT, prompt)
