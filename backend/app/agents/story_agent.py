# app/agents/story_agent.py

from llm_orchestrator import call_openai_chat


class StoryAgent:
    """Handles world-building, lore, characters, and narrative structure."""

    SYSTEM_PROMPT = """
    You are a Narrative Designer specializing in worldbuilding for mass-appeal games.
    Use modern worldbuilding frameworks and narrative efficiency.

    Produce:
    - World setting
    - Main characters (3–6)
    - Narrative tone
    - Short lore overview
    - How the story reinforces mechanics & progression
    """

    @staticmethod
    async def generate_story(game_concept, retrieved_knowledge=None):
        prompt = f"""
        Game Concept:
        {game_concept}

        Retrieved Knowledge:
        {retrieved_knowledge}
        """
        return await call_openai_chat(StoryAgent.SYSTEM_PROMPT, prompt)
