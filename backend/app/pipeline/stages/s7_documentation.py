import hashlib
import json
from pathlib import Path
from typing import Any

from app.config import settings
from app.events import schemas as ev
from app.events.bus import event_bus
from app.pipeline.context import BranchContext
from app.pipeline.stages.base_stage import BaseStage, StageOutput

DOC_TYPES = [
    "executive_summary",
    "architecture_overview",
    "component_specs",
    "requirements_traceability",
    "risk_register",
    "implementation_roadmap",
    "open_questions",
]


def _fmt(value: Any, indent: int = 0) -> str:
    """Format a value as a readable string for prompt injection."""
    if value is None:
        return "(none)"
    if isinstance(value, str):
        return value.strip() or "(empty)"
    if isinstance(value, (list, dict)):
        try:
            return json.dumps(value, indent=2, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)


def _extract_context(ctx: BranchContext) -> dict[str, str]:
    """Extract named fields from all prior stage outputs into a flat string dict."""
    s = ctx.stage_outputs

    intake = s.get("intake", {})
    feasibility = s.get("feasibility_scan", {})
    design = s.get("solution_design", {})
    analysis = s.get("solution_analysis", {})
    decomp = s.get("decomposition", {})
    deep = s.get("deep_review", {})

    # Build component list from decomposition for easy reuse
    components = decomp.get("components", [])
    comp_names = [c.get("name", "?") for c in components]
    comp_list_str = "\n".join(
        f"- {c.get('name','?')} [{c.get('priority','?')}]: {c.get('description','')}"
        for c in components
    ) or "(none)"

    return {
        "idea_name": ctx.idea_name,
        "idea_description": ctx.idea_description,
        "idea_requirements": ctx.idea_requirements,
        "idea_constraints": ctx.idea_constraints,

        # Intake
        "intake_problem_statement": _fmt(intake.get("problem_statement")),
        "intake_key_requirements": _fmt(intake.get("key_requirements")),
        "intake_key_constraints": _fmt(intake.get("key_constraints")),
        "intake_scope_notes": _fmt(intake.get("scope_notes")),

        # Feasibility
        "feasibility_warnings": _fmt(feasibility.get("warnings")),

        # Solution design
        "solution_name": design.get("solution_name") or ctx.idea_name,
        "solution_approach": _fmt(design.get("approach")),
        "key_technologies": _fmt(design.get("key_technologies")),
        "architecture_summary": _fmt(design.get("architecture_summary")),
        "major_components": _fmt(design.get("major_components")),
        "design_risks": _fmt(design.get("risks")),
        "design_reasoning": _fmt(design.get("reasoning")),

        # Solution analysis
        "requirements_coverage": _fmt(analysis.get("requirements_coverage")),
        "analysis_risks": _fmt(analysis.get("risks")),
        "analysis_gaps": _fmt(analysis.get("gaps")),

        # Decomposition
        "components_json": _fmt(components),
        "component_names": ", ".join(comp_names) if comp_names else "(none)",
        "component_list": comp_list_str,
        "dependency_notes": _fmt(decomp.get("dependency_notes")),

        # Deep review
        "review_confidence": str(deep.get("confidence", "?")),
        "review_gaps": _fmt(deep.get("gaps")),
        "review_conflicts": _fmt(deep.get("conflicts")),
        "review_unmet_requirements": _fmt(deep.get("unmet_requirements")),
        "review_recommendation": _fmt(deep.get("recommendation")),
        "review_reasoning": _fmt(deep.get("reasoning")),
    }


def _build_doc_prompt(doc_type: str, cx: dict[str, str]) -> str:
    if doc_type == "executive_summary":
        return f"""You are writing the EXECUTIVE SUMMARY for: "{cx['idea_name']}"

IDEA DESCRIPTION:
{cx['idea_description']}

REQUIREMENTS:
{cx['idea_requirements']}

CONSTRAINTS:
{cx['idea_constraints']}

CHOSEN SOLUTION: {cx['solution_name']}
SOLUTION APPROACH:
{cx['solution_approach']}

KEY TECHNOLOGIES: {cx['key_technologies']}

FINAL REVIEW CONFIDENCE: {cx['review_confidence']}
REVIEWER RECOMMENDATION:
{cx['review_recommendation']}

KEY COMPONENTS: {cx['component_names']}

IDENTIFIED RISKS:
{cx['design_risks']}

INSTRUCTIONS:
Write a 400–600 word executive summary that covers exactly:
1. Problem statement — what "{cx['idea_name']}" is trying to solve
2. The chosen solution "{cx['solution_name']}" and why this approach was selected over alternatives
3. The key technologies: {cx['key_technologies']}
4. The major components: {cx['component_names']}
5. The top risks and how they are mitigated
6. The final recommendation from the technical review

Do NOT use generic business language. Do NOT write about "staying competitive" or "business environments". Every sentence must be specific to "{cx['idea_name']}".

Return JSON with key: content (markdown string, use # headings)."""

    if doc_type == "architecture_overview":
        return f"""You are writing the ARCHITECTURE OVERVIEW for: "{cx['idea_name']}"
SOLUTION: {cx['solution_name']}

ARCHITECTURE SUMMARY:
{cx['architecture_summary']}

COMPONENTS (from decomposition stage):
{cx['component_list']}

DEPENDENCY NOTES:
{cx['dependency_notes']}

TECHNOLOGIES: {cx['key_technologies']}

COMPONENT GRAPH DATA (use these exact nodes):
{cx['components_json']}

INSTRUCTIONS:
Write an architecture overview that covers exactly:
1. A brief description of the overall architecture of "{cx['solution_name']}"
2. A Mermaid diagram showing the components and their dependencies. Use the component names exactly as listed above: {cx['component_names']}. Use the depends_on relationships from the component data above.
3. A description of each major component's role in the system
4. The technology stack and why each technology was chosen

Use the EXACT component names from the list above. Do not invent components that are not in the list.

Also return a component_graph JSON object with:
- nodes: array of {{id, name, priority}} — use the exact ids and names from the components above
- edges: array of {{from, to}} — use the depends_on relationships

Return JSON with keys:
- content (markdown string with embedded Mermaid diagram, use ``` mermaid fence)
- component_graph (object with nodes and edges arrays)"""

    if doc_type == "component_specs":
        return f"""You are writing COMPONENT SPECIFICATIONS for: "{cx['idea_name']}"
SOLUTION: {cx['solution_name']}

COMPONENTS TO SPECIFY:
{cx['component_list']}

FULL COMPONENT DATA:
{cx['components_json']}

DEPENDENCY NOTES:
{cx['dependency_notes']}

INSTRUCTIONS:
Write a detailed specification for EACH component listed above. You MUST write a section for each of these components: {cx['component_names']}

For each component, include:
- Purpose: what it does in the context of "{cx['idea_name']}"
- Inputs: what data/signals it receives
- Outputs: what data/signals it produces
- Dependencies: which other components it depends on (use depends_on from the data above)
- Implementation notes: specific technical guidance for this component
- Risks and unknowns: use the risks and unknowns from the component data above

Do NOT invent components. Use ONLY the components listed above.
Do NOT use generic descriptions. Every sentence must be specific to "{cx['idea_name']}".

Return JSON with key: content (markdown string, one ## section per component)."""

    if doc_type == "requirements_traceability":
        return f"""You are writing a REQUIREMENTS TRACEABILITY MATRIX for: "{cx['idea_name']}"

ORIGINAL REQUIREMENTS:
{cx['idea_requirements']}

ORIGINAL CONSTRAINTS:
{cx['idea_constraints']}

REQUIREMENTS COVERAGE ANALYSIS (from solution analysis stage):
{cx['requirements_coverage']}

COMPONENTS: {cx['component_names']}

UNMET REQUIREMENTS (from final review):
{cx['review_unmet_requirements']}

INSTRUCTIONS:
Write a requirements traceability matrix for "{cx['idea_name']}" that:
1. Lists each requirement from the ORIGINAL REQUIREMENTS above
2. Lists each constraint from the ORIGINAL CONSTRAINTS above
3. For each item, identifies which component(s) satisfy it (use ONLY component names: {cx['component_names']})
4. Marks items from the UNMET REQUIREMENTS list as partially covered or not covered
5. Uses the coverage data from the REQUIREMENTS COVERAGE ANALYSIS

Use a markdown table with columns: Requirement/Constraint | Type | Satisfying Components | Coverage | Notes

Do NOT invent requirements that were not stated above.

Return JSON with key: content (markdown string with table)."""

    if doc_type == "risk_register":
        return f"""You are writing a RISK REGISTER for: "{cx['idea_name']}"
SOLUTION: {cx['solution_name']}

RISKS FROM SOLUTION DESIGN:
{cx['design_risks']}

RISKS FROM SOLUTION ANALYSIS:
{cx['analysis_risks']}

GAPS FROM SOLUTION ANALYSIS:
{cx['analysis_gaps']}

GAPS FROM FINAL REVIEW:
{cx['review_gaps']}

CONFLICTS FROM FINAL REVIEW:
{cx['review_conflicts']}

FEASIBILITY WARNINGS:
{cx['feasibility_warnings']}

INSTRUCTIONS:
Compile a risk register for "{cx['idea_name']}" using ALL the risk data above. Do not invent risks that are not present in the data above.

For each risk:
- Risk: describe it in specific terms relating to "{cx['idea_name']}"
- Category: Technical | Resource | Dependency | Feasibility | Integration
- Likelihood: Low | Medium | High
- Impact: Low | Medium | High
- Mitigation: specific action to reduce the risk

Use a markdown table. Include at minimum all risks from the RISKS FROM SOLUTION DESIGN and RISKS FROM SOLUTION ANALYSIS sections above.

Return JSON with key: content (markdown string with table and brief intro)."""

    if doc_type == "implementation_roadmap":
        return f"""You are writing an IMPLEMENTATION ROADMAP for: "{cx['idea_name']}"
SOLUTION: {cx['solution_name']}

COMPONENTS WITH PRIORITIES:
{cx['component_list']}

DEPENDENCY NOTES:
{cx['dependency_notes']}

TECHNOLOGIES: {cx['key_technologies']}

REVIEWER RECOMMENDATION:
{cx['review_recommendation']}

CONSTRAINTS:
{cx['idea_constraints']}

INSTRUCTIONS:
Write an implementation roadmap for "{cx['solution_name']}" that:
1. Groups the components into phases based on their priority (P0 first, then P1, then P2) and dependency order
2. Phase 0: Foundation — components with no dependencies (or only P0 dependencies), prioritised P0
3. Phase 1: Core functionality — remaining P0 and priority P1 components
4. Phase 2: Extended features — P1 and P2 components
5. Each phase lists exactly which components are built: use names from {cx['component_names']}
6. Includes specific technical milestones derived from the component capabilities
7. Flags the constraints that must be respected throughout: {cx['idea_constraints']}

Do NOT include generic project management advice. Every milestone must name specific components.

Return JSON with key: content (markdown string, one ## section per phase)."""

    if doc_type == "open_questions":
        return f"""You are writing the OPEN QUESTIONS document for: "{cx['idea_name']}"
SOLUTION: {cx['solution_name']}

UNKNOWNS FROM COMPONENTS:
{cx['component_list']}

GAPS FROM ANALYSIS:
{cx['analysis_gaps']}

GAPS FROM FINAL REVIEW:
{cx['review_gaps']}

CONFLICTS FROM FINAL REVIEW:
{cx['review_conflicts']}

UNMET REQUIREMENTS:
{cx['review_unmet_requirements']}

INSTRUCTIONS:
Compile all unresolved questions and decisions for "{cx['idea_name']}" using the data above. Group them by category:

1. **Technical Unknowns** — from the component unknowns and gaps above
2. **Design Decisions** — choices that were flagged but not resolved during analysis
3. **Constraint Clarifications** — any ambiguities in the original constraints: {cx['idea_constraints']}
4. **Integration Questions** — from the conflicts section above
5. **Risk Mitigations Requiring Decisions** — open items from the risk analysis

For each item: state the question, state why it matters for the implementation, and suggest what information or experiment would resolve it.

Do NOT include questions that are already answered in the analysis data above.
Do NOT include generic questions like "what is the budget?" that were not raised by the analysis.

Return JSON with key: content (markdown string, ## section per category, each item as a checkbox - [ ])."""

    raise ValueError(f"Unknown doc_type: {doc_type}")


class DocumentationStage(BaseStage):
    stage_key = "documentation"
    stage_index = 7
    stage_name = "documentation"

    async def run(self, ctx: BranchContext, session) -> StageOutput:
        cx = _extract_context(ctx)

        doc_dir = settings.documents_dir / ctx.idea_id / str(ctx.branch_index)
        doc_dir.mkdir(parents=True, exist_ok=True)

        results = {}
        system_prompt = (
            f"You are a technical documentation writer for the project \"{ctx.idea_name}\". "
            "You have been given structured analysis data from a rigorous multi-stage pipeline. "
            "Your job is to write documentation that accurately reflects this specific analysis. "
            "Use only the data provided. Never use generic filler phrases like 'in today's competitive environment', "
            "'leverage synergies', or 'stay ahead of the competition'. "
            "Every sentence must be traceable to the analysis data you were given. "
            "Return valid JSON with the exact keys specified in the task."
        )

        for i, doc_type in enumerate(DOC_TYPES):
            user_prompt = _build_doc_prompt(doc_type, cx)
            try:
                output = await self._call(
                    self._build_messages(system_prompt, user_prompt), session, ctx, call_index=i
                )
                content = output.get("content", "")
                file_path = doc_dir / f"{doc_type}.md"
                file_path.write_text(content, encoding="utf-8")
                content_hash = hashlib.sha256(content.encode()).hexdigest()

                # architecture_overview also has component_graph — store it separately
                if doc_type == "architecture_overview" and "component_graph" in output:
                    graph_path = doc_dir / "component_graph.json"
                    graph_path.write_text(
                        json.dumps(output["component_graph"], indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )

                await self._save_document(session, ctx, doc_type, str(file_path), content_hash)
                await event_bus.publish(
                    ev.document_created(ctx.idea_id, ctx.branch_id, doc_type, str(file_path))
                )

                results[doc_type] = {"file_path": str(file_path), "hash": content_hash}
            except Exception as e:
                results[doc_type] = {"error": str(e)}

        return StageOutput(stage_name=self.stage_name, output=results)

    async def _save_document(
        self, session, ctx: BranchContext, doc_type: str, file_path: str, content_hash: str
    ):
        from app.db.models import Document

        doc = Document(
            idea_id=ctx.idea_id,
            branch_id=ctx.branch_id,
            doc_type=doc_type.upper(),
            file_path=file_path,
            content_hash=content_hash,
        )
        session.add(doc)
        await session.commit()
