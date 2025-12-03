# app/agents/qc_agent.py

from app.llm_client import call_openai_chat




class QCAgent:
    """
    Final multi-level reviewer.
    Applies AAA-level game design consistency checks and frameworks:
    - MDA
    - Flow theory
    - Retention loops
    - Cognitive load
    - Player types
    - Design ethics
    """

    SYSTEM_PROMPT = """
    You are the Chief Game Designer reviewing a large GDD section-by-section.
    Your job:
    1. Check for contradictions.
    2. Check clarity and cohesion.
    3. Check alignment across concept, mechanics, monetisation, story.
    4. Score each section (0–10).
    5. Suggest improvements.
    6. Return a fully polished, unified version.

    Be strict but constructive.
    """

    @staticmethod
    async def review_document(full_document, retrieved_knowledge=None):
        prompt = f"""
        FULL DOCUMENT TO REVIEW:
        {full_document}

        Extra Knowledge:
        {retrieved_knowledge}

        Review thoroughly. Provide corrections + final cleaned version.
        """
        return await call_openai_chat(QCAgent.SYSTEM_PROMPT, prompt)
