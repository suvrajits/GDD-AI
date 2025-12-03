"""
meta_agent.py

Orchestration layer for the multi-agent GDD pipeline.

Features:
- Sequential agent execution with a QC loop per-agent (ensure_quality)
- Final GDD aggregation and whole-document QC (which can route revisions to multiple agents)
- RAG grounding: pulls top-k chunks from the shared `rag` instance
- Configurable max retry attempts and which agents to run
- Clean return value containing per-section outputs + final GDD

Usage:
    from app.meta_agent import generate_gdd
    gdd_result = await generate_gdd(user_prompt, requester_name="Lead Designer")
"""

import asyncio
import logging
from typing import Dict, Any, Tuple, List, Optional

# Import your agents. Adjust paths if your project structure differs.
from app.agents.game_concept_agent import GameConceptAgent
from app.agents.mechanics_agent import MechanicsAgent
from app.agents.monetisation_agent import MonetisationAgent
from app.agents.story_agent import StoryAgent
from app.agents.aggregator_agent import AggregatorAgent
from app.agents.qc_agent import QCAgent
from app.agents.art_direction_agent import ArtDirectionAgent

# Import the global RAG instance used by routes so we ground queries consistently
from app.routes.rag_routes import rag

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# -------------------------
# Helpers: RAG + QC parsing
# -------------------------
def build_rag_context(query: str, k: int = 5) -> str:
    """
    Pull top-k results from FAISS/RAG and return concatenated context string.
    Synchronous (rag.search is sync in your engine).
    """
    try:
        results = rag.search(query, k)
        context = "\n\n".join(f"[Source: {r['meta'].get('file','')}] {r['text']}" for r in results)
        return context
    except Exception as e:
        log.exception("RAG search failed: %s", e)
        return ""


async def qc_review(section_name: str, text: str) -> str:
    """
    Call QCAgent to review `text` for `section_name`.
    Returns QC agent's free-form feedback string.
    Expectation:
        - If QCAgent returns 'APPROVED' (or contains 'APPROVED') then section is good.
        - Otherwise the feedback should contain actionable items, possibly including
          explicit routing hints like 'send to MechanicsAgent' or 'send to ArtDirectionAgent'.
    """
    try:
        feedback = await QCAgent.review(section_name, text)
        # normalize to string
        if feedback is None:
            return ""
        return feedback.strip()
    except Exception as e:
        log.exception("QCAgent.review failed for %s: %s", section_name, e)
        return "QCAgent error: " + str(e)


def parse_qc_routing(feedback: str) -> List[str]:
    """
    Inspect QC feedback for simple routing hints.
    Returns list of agent names to re-run. This is permissive and
    matches keywords like 'Mechanics', 'Art', 'Monetisation', 'Story', 'Concept'.
    QC writers should include explicit hints to help routing (this file also handles free-text).
    """
    if not feedback:
        return []

    lower = feedback.lower()
    routing = []
    if "mechanic" in lower:
        routing.append("mechanics")
    if "art" in lower or "visual" in lower or "aesthetic" in lower:
        routing.append("art")
    if "monetis" in lower or "econom" in lower:
        routing.append("monetisation")
    if "story" in lower or "narrat" in lower or "world" in lower:
        routing.append("story")
    if "concept" in lower or "core idea" in lower or "vision" in lower:
        routing.append("concept")
    # de-duplicate preserving order
    return list(dict.fromkeys(routing))


# -------------------------
# Core ensure_quality loop
# -------------------------
async def ensure_quality(
    agent_name: str,
    agent_call_coro,
    section_name: str,
    max_attempts: int = 3,
) -> Tuple[bool, str, Optional[str]]:
    """
    Generic QC loop wrapper.

    - agent_name: textual id ("mechanics", "art", "monetisation", "story", "concept")
    - agent_call_coro: an async callable that returns the agent's generated text (callable zero-arg or a lambda)
    - section_name: human-friendly section label for QC (e.g., "Mechanics")
    Returns: (approved_bool, final_text, last_qc_feedback_or_none)
    """
    qc_feedback = None
    current_output = None

    for attempt in range(1, max_attempts + 1):
        try:
            # Agent generates (agent_call_coro may embed the inputs)
            current_output = await agent_call_coro(qc_feedback)
        except TypeError:
            # If agent_call_coro expects no qc_feedback param
            current_output = await agent_call_coro()

        if current_output is None:
            current_output = ""

        # Ask QC to review
        feedback = await qc_review(section_name, current_output)
        log.info("QC feedback for %s (attempt %s): %s", section_name, attempt, feedback)

        # If QC explicitly says approved (case-insensitive), we accept
        if "approved" in (feedback or "").lower() or feedback.strip() == "":
            return True, current_output, None

        # Otherwise, prepare to send feedback back to agent and retry
        qc_feedback = feedback
        # Small backoff between attempts (avoid hitting rate-limits)
        await asyncio.sleep(0.5 * attempt)

    # After retries exhausted
    return False, current_output or "", qc_feedback


# -------------------------
# Agent wrappers
# -------------------------
# Each wrapper returns an async callable that the ensure_quality expects:
# agent_call_coro(qc_feedback) -> returns generated_text

def concept_agent_callable(user_prompt: str, rag_context: str):
    async def _call(qc_feedback: Optional[str] = None):
        # GameConceptAgent.generate(concept_prompt, retrieved_knowledge="")
        return await GameConceptAgent.generate(
            user_prompt,
            retrieved_knowledge=rag_context,
            qc_feedback=qc_feedback,
        )
    return _call


def mechanics_agent_callable(concept_text: str, rag_context: str):
    async def _call(qc_feedback: Optional[str] = None):
        return await MechanicsAgent.generate(
            concept=concept_text,
            retrieved_knowledge=rag_context,
            qc_feedback=qc_feedback,
        )
    return _call


def monetisation_agent_callable(concept_text: str, mechanics_text: str, rag_context: str):
    async def _call(qc_feedback: Optional[str] = None):
        return await MonetisationAgent.generate(
            concept=concept_text,
            mechanics=mechanics_text,
            retrieved_knowledge=rag_context,
            qc_feedback=qc_feedback,
        )
    return _call


def story_agent_callable(concept_text: str, mechanics_text: str, rag_context: str):
    async def _call(qc_feedback: Optional[str] = None):
        return await StoryAgent.generate(
            concept=concept_text,
            mechanics=mechanics_text,
            retrieved_knowledge=rag_context,
            qc_feedback=qc_feedback,
        )
    return _call


def art_agent_callable(concept_text: str, mechanics_text: str, story_text: str, monetisation_text: str, rag_context: str):
    async def _call(qc_feedback: Optional[str] = None):
        return await ArtDirectionAgent.generate(
            concept=concept_text,
            mechanics=mechanics_text,
            story=story_text,
            monetisation=monetisation_text,
            retrieved_knowledge=rag_context,
            qc_feedback=qc_feedback,
        )
    return _call


# -------------------------
# High-level orchestration
# -------------------------
async def generate_gdd(
    user_prompt: str,
    requester_name: str = "Lead Designer",
    rag_k: int = 5,
    max_attempts_per_agent: int = 3,
) -> Dict[str, Any]:
    """
    Orchestrate the full GDD pipeline:
    1. Build RAG context from the user prompt
    2. Run Concept -> QC loop
    3. Run Mechanics -> QC loop
    4. Run Monetisation -> QC loop
    5. Run Story -> QC loop
    6. Run Art Direction -> QC loop
    7. Aggregate into a GDD
    8. Final QCAgent review the whole GDD; if QC requests changes for specific agents,
       re-run those agents (with their own QC loops), re-aggregate, and re-review until satisfied.

    Returns a dict:
    {
        "concept": "...",
        "mechanics": "...",
        "monetisation": "...",
        "story": "...",
        "art_direction": "...",
        "aggregated_gdd": "...",
        "final_qc_feedback": "...",
        "status": "ok" or "failed"
    }
    """
    log.info("Starting GDD generation for requester: %s", requester_name)
    rag_context = build_rag_context(user_prompt, k=rag_k)

    state: Dict[str, str] = {
        "concept": "",
        "mechanics": "",
        "monetisation": "",
        "story": "",
        "art_direction": "",
    }

    # 1) Concept
    ok, out, fb = await ensure_quality(
        agent_name="concept",
        agent_call_coro=concept_agent_callable(user_prompt, rag_context),
        section_name="Game Concept",
        max_attempts=max_attempts_per_agent,
    )
    if not ok:
        return {"status": "failed", "reason": "Concept failed QC", "concept": out, "qc_feedback": fb}
    state["concept"] = out

    # refresh rag context using the approved concept (optional: improves grounding)
    rag_context = build_rag_context(state["concept"], k=rag_k)

    # 2) Mechanics
    ok, out, fb = await ensure_quality(
        agent_name="mechanics",
        agent_call_coro=mechanics_agent_callable(state["concept"], rag_context),
        section_name="Mechanics",
        max_attempts=max_attempts_per_agent,
    )
    if not ok:
        return {"status": "failed", "reason": "Mechanics failed QC", "mechanics": out, "qc_feedback": fb}
    state["mechanics"] = out

    # 3) Monetisation
    ok, out, fb = await ensure_quality(
        agent_name="monetisation",
        agent_call_coro=monetisation_agent_callable(state["concept"], state["mechanics"], rag_context),
        section_name="Monetisation",
        max_attempts=max_attempts_per_agent,
    )
    if not ok:
        return {"status": "failed", "reason": "Monetisation failed QC", "monetisation": out, "qc_feedback": fb}
    state["monetisation"] = out

    # 4) Story
    ok, out, fb = await ensure_quality(
        agent_name="story",
        agent_call_coro=story_agent_callable(state["concept"], state["mechanics"], rag_context),
        section_name="Story",
        max_attempts=max_attempts_per_agent,
    )
    if not ok:
        return {"status": "failed", "reason": "Story failed QC", "story": out, "qc_feedback": fb}
    state["story"] = out

    # 5) Art Direction
    ok, out, fb = await ensure_quality(
        agent_name="art",
        agent_call_coro=art_agent_callable(state["concept"], state["mechanics"], state["story"], state["monetisation"], rag_context),
        section_name="Art Direction",
        max_attempts=max_attempts_per_agent,
    )
    if not ok:
        return {"status": "failed", "reason": "Art Direction failed QC", "art_direction": out, "qc_feedback": fb}
    state["art_direction"] = out

    # 6) Aggregation
    try:
        aggregated = await AggregatorAgent.generate(
            concept=state["concept"],
            mechanics=state["mechanics"],
            monetisation=state["monetisation"],
            story=state["story"],
            art_direction=state["art_direction"],
            retrieved_knowledge=rag_context,
        )
    except Exception as e:
        log.exception("Aggregator failed: %s", e)
        return {"status": "failed", "reason": "Aggregator error", "error": str(e)}

    # 7) Final QC on whole GDD
    final_feedback = await qc_review("Full GDD", aggregated)
    log.info("Final QC feedback: %s", final_feedback)

    # If final feedback indicates approval, complete
    if "approved" in (final_feedback or "").lower() or final_feedback.strip() == "":
        return {
            "status": "ok",
            "concept": state["concept"],
            "mechanics": state["mechanics"],
            "monetisation": state["monetisation"],
            "story": state["story"],
            "art_direction": state["art_direction"],
            "aggregated_gdd": aggregated,
            "final_qc_feedback": final_feedback,
        }

    # Otherwise parse routing hints and re-run requested agents (one or many)
    routing = parse_qc_routing(final_feedback)
    log.info("Final QC requests reruns for: %s", routing)

    # If QC didn't provide helpful routing hints, treat it as "review all" (safe default)
    if not routing:
        routing = ["concept", "mechanics", "monetisation", "story", "art"]

    # Re-run the requested agents in the same pattern: ensure_quality for each
    for agent_key in routing:
        if agent_key == "concept":
            ok, out, fb = await ensure_quality(
                agent_name="concept",
                agent_call_coro=concept_agent_callable(user_prompt, rag_context),
                section_name="Game Concept",
                max_attempts=max_attempts_per_agent,
            )
            state["concept"] = out if ok else state["concept"]
        elif agent_key == "mechanics":
            ok, out, fb = await ensure_quality(
                agent_name="mechanics",
                agent_call_coro=mechanics_agent_callable(state["concept"], rag_context),
                section_name="Mechanics",
                max_attempts=max_attempts_per_agent,
            )
            state["mechanics"] = out if ok else state["mechanics"]
        elif agent_key == "monetisation":
            ok, out, fb = await ensure_quality(
                agent_name="monetisation",
                agent_call_coro=monetisation_agent_callable(state["concept"], state["mechanics"], rag_context),
                section_name="Monetisation",
                max_attempts=max_attempts_per_agent,
            )
            state["monetisation"] = out if ok else state["monetisation"]
        elif agent_key == "story":
            ok, out, fb = await ensure_quality(
                agent_name="story",
                agent_call_coro=story_agent_callable(state["concept"], state["mechanics"], rag_context),
                section_name="Story",
                max_attempts=max_attempts_per_agent,
            )
            state["story"] = out if ok else state["story"]
        elif agent_key == "art":
            ok, out, fb = await ensure_quality(
                agent_name="art",
                agent_call_coro=art_agent_callable(state["concept"], state["mechanics"], state["story"], state["monetisation"], rag_context),
                section_name="Art Direction",
                max_attempts=max_attempts_per_agent,
            )
            state["art_direction"] = out if ok else state["art_direction"]

    # Re-aggregate after re-runs
    try:
        aggregated = await AggregatorAgent.generate(
            concept=state["concept"],
            mechanics=state["mechanics"],
            monetisation=state["monetisation"],
            story=state["story"],
            art_direction=state["art_direction"],
            retrieved_knowledge=rag_context,
        )
    except Exception as e:
        log.exception("Aggregator failed on re-aggregate: %s", e)
        return {"status": "failed", "reason": "Aggregator re-aggregate error", "error": str(e)}

    # One final QC pass
    final_feedback = await qc_review("Full GDD", aggregated)
    if "approved" in (final_feedback or "").lower() or final_feedback.strip() == "":
        return {
            "status": "ok",
            "concept": state["concept"],
            "mechanics": state["mechanics"],
            "monetisation": state["monetisation"],
            "story": state["story"],
            "art_direction": state["art_direction"],
            "aggregated_gdd": aggregated,
            "final_qc_feedback": final_feedback,
        }

    # If still not approved after re-run, return a 'requires human intervention' result
    return {
        "status": "failed",
        "reason": "Final QC did not approve after automated re-runs",
        "aggregated_gdd": aggregated,
        "final_qc_feedback": final_feedback,
        "state": state,
    }
