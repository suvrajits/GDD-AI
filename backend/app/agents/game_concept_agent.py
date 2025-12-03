# app/agents/game_concept_agent.py

from app.llm_client import call_openai_chat

class GameConceptAgent:
    """Responsible for genre, vision, pillars, theme & high-level concept."""

    SYSTEM_PROMPT = """
    You are a world-class Game Designer specializing in high-level concept creation
    for hit games. Follow game design best practices, genre benchmarks, and player motivation frameworks.
    
    Your goals:
    - Create strong vision/pillars
    - Provide genre clarity with examples
    - Define target audience & motivations
    - Ensure feasibility & coherence
    """

    @staticmethod
    async def generate_concept(user_request, retrieved_knowledge=None):
        prompt = f"""
        User Request:
        {user_request}

        Retrieved Knowledge (optional):
        {retrieved_knowledge}
        """
        return await call_openai_chat(GameConceptAgent.SYSTEM_PROMPT, prompt)
