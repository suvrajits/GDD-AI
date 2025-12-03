# app/agents/monetisation_agent.py

from app.llm_client import call_openai_chat




class MonetisationAgent:
    """Designs ethical yet profitable monetisation systems."""

    SYSTEM_PROMPT = """
    You are a Monetisation Designer with expertise in:
    - Hybrid Casual IAPs
    - Ads + Rewarded Video
    - Economic pacing
    - Currency sinks & sources
    - Ethical Design (NO predatory loops)

    Follow Economy Design Frameworks + Retention Loops.
    """

    @staticmethod
    async def generate_monetisation(game_concept, mechanics, retrieved_knowledge=None):
        prompt = f"""
        Game Concept:
        {game_concept}

        Mechanics:
        {mechanics}

        Retrieved Knowledge:
        {retrieved_knowledge}
        """
        return await call_openai_chat(MonetisationAgent.SYSTEM_PROMPT, prompt)
