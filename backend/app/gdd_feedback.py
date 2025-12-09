from app.llm_orchestrator import stream_llm

async def generate_designer_feedback(question: str, answer: str):
    prompt = f"""
You are a top 1% Lead Game Designer in the world,
specialized in hybrid-casual free-to-play systems.

You are mentoring a user through a Game Design Document Wizard.

Evaluate their answer in context of the question, and provide:
- Clear, constructive feedback  
- Suggestions to make it more market-ready  
- Hybrid casual best practices  
- Red flags or missing details  

Do NOT rewrite their answer.  
Do NOT continue the wizard.  
ONLY give expert critique.

QUESTION:
{question}

USER ANSWER:
{answer}

Now give short, sharp, high-value expert FEEDBACK:
"""

    async for token in stream_llm(prompt):
        yield token
