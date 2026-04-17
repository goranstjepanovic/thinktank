"""
Phase 2 conversation agent.

Drives the interactive session that bridges the Phase 1 analysis (selected solution
documentation) and Phase 2 implementation. The agent:

  1. On session start: reads the Open Questions doc from the selected branch,
     builds a rich system prompt, and generates an opening message that presents
     the questions and frames the work ahead.
  2. On each user turn: builds the full conversation history, calls the model
     with a free-form text response (no JSON), and returns the assistant reply.
"""

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, Idea, Phase2Message, Phase2Session, SolutionBranch
from app.inference.base import Message
from app.inference.client import InferenceClient

logger = logging.getLogger(__name__)

STAGE_KEY = "phase2_conversation"

# ---------------------------------------------------------------------------
# System prompt helpers
# ---------------------------------------------------------------------------

def _build_system_prompt(idea: Idea, branch: SolutionBranch, open_questions: str) -> str:
    return f"""You are an expert implementation assistant for the Think Tank system.

You are helping the user implement their selected solution for the following idea:

IDEA: {idea.name}
DESCRIPTION:
{idea.description}

REQUIREMENTS:
{idea.requirements}

CONSTRAINTS:
{idea.constraints}

SELECTED SOLUTION (Branch {branch.branch_index}):
{branch.approach_summary or "No approach summary available."}

---

OPEN QUESTIONS IDENTIFIED DURING ANALYSIS:
{open_questions}

---

YOUR ROLE IN PHASE 2:

Phase 2 has two steps:
1. **Resolution** (now): Work through the Open Questions above with the user. Ask for the \
information you need. Accept additional context, preferences, and constraints. Once all \
blocking questions are resolved, confirm readiness and tell the user to click "Proceed to implementation".

2. **Implementation** (next): Generate concrete implementation artifacts — working code, \
scaffolding, configuration, tests, documentation — step by step. Each artifact is reviewed \
before proceeding.

GUIDELINES:
- Be direct and concrete. Propose specific solutions, not just options.
- Respect all constraints absolutely — treat them as hard requirements.
- When you need a decision, ask clearly and wait for the answer.
- Format responses in clean Markdown.
- When generating code, provide complete, working implementations (no pseudocode or stubs).
- Reference the idea's requirements and constraints when making decisions.
"""


def _build_opening_prompt(idea: Idea, branch: SolutionBranch, open_questions: str) -> list[Message]:
    system = _build_system_prompt(idea, branch, open_questions)
    user = (
        f"I've selected Branch {branch.branch_index} as the solution to develop. "
        f"Let's begin Phase 2. Please introduce the open questions we need to resolve "
        f"before implementation can start, and ask me the most important one first."
    )
    return [Message(role="system", content=system), Message(role="user", content=user)]


def _build_conversation(
    idea: Idea,
    branch: SolutionBranch,
    open_questions: str,
    history: list[Phase2Message],
    new_user_message: str,
    resolution_summary: str | None = None,
) -> list[Message]:
    system = _build_system_prompt(idea, branch, open_questions)

    # If a resolution summary exists (session is READY or beyond), inject it after the
    # system prompt so the model has the structured decisions without scanning the full
    # conversation transcript. The raw history is still included for continuity.
    messages: list[Message] = [Message(role="system", content=system)]
    if resolution_summary:
        messages.append(Message(
            role="system",
            content=(
                "## Resolution Summary\n"
                "The following decisions were captured during the Q&A phase. "
                "These take precedence over anything ambiguous in the conversation below.\n\n"
                + resolution_summary
            ),
        ))

    for m in history:
        messages.append(Message(role=m.role, content=m.content))
    messages.append(Message(role="user", content=new_user_message))
    return messages


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class Phase2Agent:
    def __init__(self, inference_client: InferenceClient) -> None:
        self._client = inference_client

    async def generate_opening(
        self,
        db: AsyncSession,
        session: Phase2Session,
        idea: Idea,
        branch: SolutionBranch,
    ) -> str:
        """Generate the opening assistant message for a new Phase 2 session."""
        open_questions = await self._load_open_questions(db, branch)
        messages = _build_opening_prompt(idea, branch, open_questions)

        return await self._client.call_text(
            stage_key=STAGE_KEY,
            messages=messages,
            session=db,
            idea_id=idea.id,
            branch_id=branch.id,
            call_type="PHASE2",
            call_index=0,
        )

    async def build_conversation_messages(
        self,
        db: AsyncSession,
        idea: Idea,
        branch: SolutionBranch,
        history: list[Phase2Message],
        user_message: str,
        resolution_summary: str | None = None,
    ) -> list[Message]:
        """Return the full message list ready for inference (system + history + new user turn)."""
        open_questions = await self._load_open_questions(db, branch)
        return _build_conversation(idea, branch, open_questions, history, user_message, resolution_summary)

    async def respond(
        self,
        db: AsyncSession,
        session: Phase2Session,
        idea: Idea,
        branch: SolutionBranch,
        history: list[Phase2Message],
        user_message: str,
    ) -> str:
        """Generate an assistant response to the latest user message."""
        open_questions = await self._load_open_questions(db, branch)
        messages = _build_conversation(idea, branch, open_questions, history, user_message)

        return await self._client.call_text(
            stage_key=STAGE_KEY,
            messages=messages,
            session=db,
            idea_id=idea.id,
            branch_id=branch.id,
            call_type="PHASE2",
            call_index=len(history),
        )

    async def generate_resolution_summary(
        self,
        db: AsyncSession,
        session: Phase2Session,
        idea: Idea,
        branch: SolutionBranch,
    ) -> str:
        """
        Synthesise the full Q&A conversation into a structured Resolution Summary.
        Called when the user marks the session as READY.

        The summary replaces the raw conversation as the authoritative context
        for all subsequent implementation calls — it is compact, structured, and
        unambiguous, avoiding context window pressure during code generation.
        """
        open_questions = await self._load_open_questions(db, branch)

        # Build the full conversation history as a readable transcript
        all_msgs = sorted(session.messages, key=lambda m: m.created_at)
        transcript = "\n\n".join(
            f"{'USER' if m.role == 'user' else 'ASSISTANT'}: {m.content}"
            for m in all_msgs
        )

        system_prompt = (
            f"You are a technical analyst synthesising a Phase 2 Q&A session into a "
            f"structured decision document. This document will be used as the sole "
            f"context for implementation — it must be complete, precise, and unambiguous.\n\n"
            f"IDEA: {idea.name}\n"
            f"DESCRIPTION: {idea.description}\n"
            f"REQUIREMENTS: {idea.requirements}\n"
            f"CONSTRAINTS: {idea.constraints}\n\n"
            f"SELECTED SOLUTION (Branch {branch.branch_index}): {branch.approach_summary or 'N/A'}\n\n"
            f"ORIGINAL OPEN QUESTIONS FROM ANALYSIS:\n{open_questions}"
        )
        user_prompt = (
            f"Below is the full Phase 2 Q&A transcript. Extract and structure ALL decisions, "
            f"answers, preferences, constraints, and clarifications the user provided.\n\n"
            f"--- TRANSCRIPT ---\n{transcript}\n--- END TRANSCRIPT ---\n\n"
            f"Produce a Resolution Summary with these sections:\n"
            f"## Resolved Questions\n"
            f"For each Open Question: state the question, then the user's answer/decision.\n\n"
            f"## Additional Constraints & Preferences\n"
            f"Any new constraints, preferences, or requirements the user introduced beyond the original idea.\n\n"
            f"## Key Technical Decisions\n"
            f"Concrete technology choices, architecture decisions, and implementation preferences "
            f"stated or agreed during the conversation.\n\n"
            f"## Open Items\n"
            f"Anything still unresolved or flagged as needing a decision during implementation.\n\n"
            f"Be precise and concrete. Quote the user's own words where the exact wording matters. "
            f"Do not summarise vaguely — this document drives code generation."
        )

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt),
        ]

        return await self._client.call_text(
            stage_key=STAGE_KEY,
            messages=messages,
            session=db,
            idea_id=idea.id,
            branch_id=branch.id,
            call_type="PHASE2",
            call_index=len(all_msgs),
        )

    # ------------------------------------------------------------------

    async def _load_open_questions(self, db: AsyncSession, branch: SolutionBranch) -> str:
        result = await db.execute(
            select(Document).where(
                Document.branch_id == branch.id,
                Document.doc_type == "OPEN_QUESTIONS",
            )
        )
        doc = result.scalar_one_or_none()
        if doc is None:
            return "No open questions document found for this solution branch."
        try:
            return Path(doc.file_path).read_text(encoding="utf-8")
        except Exception:
            return "Could not load the open questions document."
