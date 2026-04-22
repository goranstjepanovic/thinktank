"""
PRD-Only Agent — Phase 3 "prd_only" mode.

Generates a single, comprehensive, portable PRD.md from the full Phase 1 + Phase 2 context.
The document is intended to be self-contained enough to hand to any developer or AI coding
tool (Claude Code, Cursor, etc.) without needing to see the earlier pipeline output.

Sections are generated individually (like CodeGeneratorAgent.generate_prd) to stay within
local model output-token limits. The tech stack section uses web_search to pin current
stable versions rather than relying on potentially stale training data.
"""

import logging
from pathlib import Path
from typing import Callable, Awaitable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, Idea, Phase2Session, Phase3Session, SolutionBranch
from app.inference.base import Message
from app.inference.client import InferenceClient

logger = logging.getLogger(__name__)

PRD_STAGE_KEY = "phase3_prd"
OUTPUT_PATH = "PRD.md"

_MAX_DOC_CHARS = 12_000

# (section_name, scope, which_context_docs_to_include)
_SECTIONS: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "Overview",
        "What this project is, the core problem it solves, who will use it, and the primary goals.",
        (),
    ),
    (
        "Functional Requirements",
        "All features the system must support, grouped logically and written as concrete requirements. "
        "Include priority levels (must-have / should-have / nice-to-have) where known.",
        ("resolution_summary",),
    ),
    (
        "Non-Functional Requirements",
        "Performance, scalability, reliability, security, maintainability, and usability targets "
        "with measurable thresholds where possible.",
        ("resolution_summary",),
    ),
    (
        "Constraints",
        "Technical, resource, integration, and timeline constraints that bound the solution. "
        "Explain why each constraint exists.",
        ("resolution_summary",),
    ),
    (
        "Architecture",
        "The selected solution approach: major layers or services, how they communicate, "
        "data flow from input to output, and the key architectural decisions with their rationale.",
        ("resolution_summary", "architecture_doc"),
    ),
    (
        "Logic Specification",
        "The complete, unambiguous definition of the system's core logic — written so that an "
        "implementer can write it as pure functions with no UI knowledge. Include:\n"
        "- Entities: every object the system tracks, with its exact state fields and types\n"
        "- Rules: every rule as an explicit condition → outcome statement "
        "(e.g. 'if switch A is ON and switch B is OFF then bulb X is ON'). No vague language.\n"
        "- Evaluation: the exact sequence in which rules are applied each tick/event\n"
        "- Win / terminal conditions: the precise predicate that ends or completes a session\n"
        "- Edge cases: what happens on invalid input, ties, boundary values\n"
        "Do NOT mention buttons, DOM, CSS, rendering, or any UI concept in this section. "
        "Logic must be expressible as pure functions that take state and return new state.",
        ("resolution_summary", "component_specs"),
    ),
    (
        "Component Specifications",
        "Each component or service in detail: its single responsibility, "
        "inputs and outputs, public interfaces or API contracts, and internal dependencies.",
        ("resolution_summary", "component_specs"),
    ),
    (
        "Tech Stack",
        "Every library, framework, runtime, tool, and infrastructure dependency required. "
        "List the specific version to use for each. "
        "Use web_search to verify each major dependency's current stable version before listing it — "
        "do not rely on training-data version numbers.",
        ("resolution_summary",),
    ),
    (
        "Data Models",
        "All persistent data entities: fields, types, constraints, relationships, and any "
        "indexing or validation rules. Include an ER diagram in Mermaid if helpful.",
        ("component_specs", "architecture_doc"),
    ),
    (
        "API Reference",
        "Every HTTP endpoint or public interface the system exposes: method, path, "
        "request body/query params, response shape, and error codes. "
        "If there is no HTTP API, describe the equivalent public interface.",
        ("component_specs",),
    ),
    (
        "Implementation Phases",
        "Ordered development milestones from initial scaffold to a production-ready system. "
        "Each phase should be independently deliverable. List tasks and dependencies.",
        ("roadmap_doc",),
    ),
    (
        "Project Structure",
        "Recommended directory and file layout with a one-line description of each file and folder. "
        "Show as a tree. Be specific — include key filenames, not just folder names.",
        (),
    ),
    (
        "Setup & Development Guide",
        "Step-by-step: prerequisites, installing dependencies, configuring the environment "
        "(which env vars, where to get values), running the project locally, and running tests.",
        (),
    ),
    (
        "Design Decisions",
        "The key choices made during analysis and Q&A with their rationale and the alternatives "
        "that were considered and rejected. Write this as a decision log.",
        ("resolution_summary", "architecture_doc"),
    ),
]

_TECH_STACK_SECTION = "Tech Stack"


def _section_system_prompt() -> str:
    return (
        "You are a technical writer and software architect producing a section of a standalone PRD.\n\n"
        "This PRD is the single source of truth for anyone implementing this project — including "
        "AI coding assistants. Write for a developer who has never seen any prior analysis output.\n\n"
        "Output ONLY raw Markdown for the requested section, starting with its `##` heading. "
        "Be specific and concrete — no vague placeholders, no 'TBD', no generic filler. "
        "No preamble, no prose outside the section, no code fences around the output."
    )


def _section_user_prompt(
    idea: Idea,
    branch: SolutionBranch,
    resolution_summary: str,
    architecture_doc: str,
    component_specs: str,
    roadmap_doc: str,
    section_name: str,
    section_scope: str,
    relevant_docs: tuple[str, ...],
) -> str:
    def _trunc(text: str) -> str:
        return text if len(text) <= _MAX_DOC_CHARS else text[:_MAX_DOC_CHARS] + "\n... (truncated)"

    doc_blocks: list[str] = []
    if "resolution_summary" in relevant_docs:
        doc_blocks.append(f"PHASE 2 DECISIONS & CONTEXT:\n{_trunc(resolution_summary)}")
    if "architecture_doc" in relevant_docs:
        doc_blocks.append(f"ARCHITECTURE:\n{_trunc(architecture_doc)}")
    if "component_specs" in relevant_docs:
        doc_blocks.append(f"COMPONENT SPECIFICATIONS:\n{_trunc(component_specs)}")
    if "roadmap_doc" in relevant_docs:
        doc_blocks.append(f"IMPLEMENTATION ROADMAP:\n{_trunc(roadmap_doc)}")

    docs_section = ("\n\n".join(doc_blocks) + "\n\n") if doc_blocks else ""

    return (
        f"PROJECT: {idea.name}\n"
        f"DESCRIPTION:\n{idea.description}\n\n"
        f"REQUIREMENTS:\n{idea.requirements}\n\n"
        f"CONSTRAINTS:\n{idea.constraints}\n\n"
        f"SELECTED SOLUTION (Branch {branch.branch_index}):\n"
        f"{branch.approach_summary or 'N/A'}\n\n"
        f"{docs_section}"
        f"Write ONLY the `## {section_name}` section of the PRD.\n"
        f"Scope: {section_scope}"
    )


async def _load_context(db: AsyncSession, session: Phase3Session, idea: Idea) -> dict[str, str]:
    """Load Phase 2 resolution summary and Phase 1 branch documents."""
    p2_r = await db.execute(select(Phase2Session).where(Phase2Session.id == session.phase2_session_id))
    phase2 = p2_r.scalar_one_or_none()
    resolution_summary = getattr(phase2, "resolution_summary", "") or ""

    branch_id = session.branch_id
    docs_r = await db.execute(select(Document).where(Document.branch_id == branch_id))
    docs = docs_r.scalars().all()

    def _get(slug: str) -> str:
        for d in docs:
            if d.doc_type == slug:
                return d.content or ""
        return ""

    return {
        "resolution_summary": resolution_summary,
        "architecture_doc": _get("architecture_overview"),
        "component_specs": _get("component_specs"),
        "roadmap_doc": _get("implementation_roadmap"),
    }


OnToolResult = Callable[[str, dict], Awaitable[None]]


class PrdOnlyAgent:
    def __init__(self, client: InferenceClient) -> None:
        self._client = client

    async def run(
        self,
        db: AsyncSession,
        session: Phase3Session,
        idea: Idea,
        branch: SolutionBranch,
        on_tool_result: OnToolResult,
    ) -> bool:
        """
        Generate a standalone PRD.md in the session output directory.
        Returns True on success, False if the file could not be written.
        """
        output_dir = session.output_dir or ""
        out_path = Path(output_dir) / OUTPUT_PATH
        out_path.parent.mkdir(parents=True, exist_ok=True)

        ctx = await _load_context(db, session, idea)

        system = _section_system_prompt()
        sections: list[str] = []

        sections.append(f"# PRD: {idea.name}\n")

        for section_name, section_scope, relevant_docs in _SECTIONS:
            logger.info("prd_only: generating section '%s'", section_name)

            user_prompt = _section_user_prompt(
                idea, branch,
                ctx["resolution_summary"], ctx["architecture_doc"],
                ctx["component_specs"], ctx["roadmap_doc"],
                section_name, section_scope, relevant_docs,
            )
            messages = [
                Message(role="system", content=system),
                Message(role="user", content=user_prompt),
            ]

            try:
                if section_name == _TECH_STACK_SECTION:
                    # Use tools so the agent can web_search for current versions
                    text = await self._client.call_with_tools(
                        stage_key=PRD_STAGE_KEY,
                        messages=messages,
                        session=db,
                        idea_id=idea.id,
                        branch_id=branch.id,
                        max_tool_rounds=6,
                        return_json=False,
                        on_tool_result=on_tool_result,
                    )
                    if not isinstance(text, str):
                        text = str(text)
                else:
                    text = await self._client.call_text(
                        stage_key=PRD_STAGE_KEY,
                        messages=messages,
                        session=db,
                        idea_id=idea.id,
                        branch_id=branch.id,
                    )
            except Exception as exc:
                logger.warning("prd_only: section '%s' failed: %s", section_name, exc)
                text = f"## {section_name}\n\n*Section generation failed: {exc}*\n"

            sections.append(text.strip())

        full_prd = "\n\n---\n\n".join(sections)

        try:
            out_path.write_text(full_prd, encoding="utf-8")
            logger.info("prd_only: wrote %s (%d chars)", out_path, len(full_prd))
            await on_tool_result("file_edit", {
                "path": OUTPUT_PATH,
                "operation": "write_file",
                "success": True,
                "size_bytes": len(full_prd.encode("utf-8")),
                "detail": "PRD.md written",
            })
            return True
        except Exception as exc:
            logger.error("prd_only: failed to write PRD: %s", exc)
            return False
