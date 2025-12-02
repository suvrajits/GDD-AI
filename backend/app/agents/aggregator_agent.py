# app/agents/aggregator_agent.py

class AggregatorAgent:
    """
    Combines outputs from all specialised agents into a structured GDD.
    """

    @staticmethod
    def assemble(concept, mechanics, monetisation, story):
        return f"""
        ======================
        GAME DESIGN DOCUMENT
        ======================

        ### 1. Game Concept
        {concept}

        ### 2. Mechanics & Systems
        {mechanics}

        ### 3. Monetisation
        {monetisation}

        ### 4. Story & Worldbuilding
        {story}

        ======================
        END OF DOCUMENT
        ======================
        """
