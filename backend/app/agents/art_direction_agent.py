# backend/app/agents/art_direction_agent.py

from app.utils import call_openai_chat

class ArtDirectionAgent:
    """
    Agent responsible for defining the Art & Visual Direction of the GDD.
    This includes: art style, aesthetic pillars, character visuals, 
    environment look, camera style, color palettes, UI/UX look & feel,
    animations, VFX direction, and mood references.
    """

    SYSTEM_PROMPT = """
    You are an award-winning Art Director for top-grossing mobile and hybrid-casual games.
    Your task is to define the complete Art & Visual Direction for the game.

    Follow these principles:
    - Be visually descriptive
    - Mention inspirations only when appropriate
    - Provide concrete stylistic guidelines
    - Keep everything aligned with the game's concept & mechanics
    - Ensure art direction supports the narrative and gameplay clarity

    Structure your output with the following sections:

    🎨 ART DIRECTION DOCUMENT

    1. **Aesthetic Overview**
        - Core look & mood
        - Inspirations / comparable titles

    2. **Art Style Pillars**
        - 3–5 high-level visual pillars
        - Why they matter

    3. **Camera & Presentation**
        - Camera angle
        - Player perspective
        - Scene composition rules

    4. **Characters & Creatures**
        - Silhouette philosophy
        - Shapes / proportions
        - Expression, costumes, motion style

    5. **Environment & World**
        - Palette
        - Lighting rules
        - Materials / textures
        - Level landmarks
        - Surface details (low, mid, high frequency)

    6. **UI / UX Direction**
        - UI theme & visual grammar
        - Iconography style
        - Color hierarchy (primary, secondary, danger)
        - Motion, transitions

    7. **VFX & Animation Style**
        - Attack VFX
        - Hit impacts
        - Movement animations
        - Timing rules

    8. **Art Production Guidelines**
        - Asset consistency rules
        - What to avoid
        - Technical considerations for mobile/hybrid-casual

    Keep your tone vivid, expert, and production-focused.
    """

    @staticmethod
    async def generate(concept: str, mechanics: str, story: str, monetisation: str, retrieved_knowledge: str = ""):
        """
        Generate Art Direction based on previously created sections + RAG knowledge.
        """

        prompt = f"""
        The following is the current state of the Game Design Document:

        --- GAME CONCEPT ---
        {concept}

        --- MECHANICS ---
        {mechanics}

        --- STORY / WORLD ---
        {story}

        --- MONETISATION ---
        {monetisation}

        --- RETRIEVED KNOWLEDGE (RAG) ---
        {retrieved_knowledge}

        Create the best possible Art Direction for this game,
        ensuring everything matches gameplay, tone, and target platform.
        """

        return await call_openai_chat(ArtDirectionAgent.SYSTEM_PROMPT, prompt)
