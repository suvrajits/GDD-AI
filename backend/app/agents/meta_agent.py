# app/agents/meta_agent.py

from .game_concept_agent import GameConceptAgent
from .mechanics_agent import MechanicsAgent
from .monetisation_agent import MonetisationAgent
from .story_agent import StoryAgent
from .qc_agent import QCAgent
from .aggregator_agent import AggregatorAgent
from rag_engine import retrieve_relevant_chunks


class MetaAgent:
    """
    Master orchestrator.
    Controls the full multi-agent flow.
    """

    @staticmethod
    async def build_gdd(user_request):
        # Step 0 — Retrieve grounding knowledge
        retrieved = await retrieve_relevant_chunks(user_request)

        # Step 1 — Concept
        concept = await GameConceptAgent.generate_concept(user_request, retrieved)

        # Step 2 — Mechanics
        mechanics = await MechanicsAgent.generate_mechanics(concept, retrieved)

        # Step 3 — Monetisation
        monetisation = await MonetisationAgent.generate_monetisation(concept, mechanics, retrieved)

        # Step 4 — Story
        story = await StoryAgent.generate_story(concept, retrieved)

        # Step 5 — Assemble document
        raw_document = AggregatorAgent.assemble(concept, mechanics, monetisation, story)

        # Step 6 — Quality Review
        final_document = await QCAgent.review_document(raw_document, retrieved)

        return final_document
