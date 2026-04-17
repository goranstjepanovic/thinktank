from app.pipeline.context import BranchContext
from app.pipeline.stages.base_stage import BaseStage, StageOutput


class DecompositionStage(BaseStage):
    stage_key = "decomposition"
    stage_index = 4
    stage_name = "decomposition"

    async def run(self, ctx: BranchContext, session) -> StageOutput:
        system_prompt = (
            "You are a systems architect. Break the proposed solution into a complete component tree. "
            "Each component must be discrete, implementable, and have clear dependencies. "
            "Return JSON with keys: components (list of {id, name, description, "
            "priority: P0|P1|P2, depends_on: [component ids], risks: [strings], "
            "unknowns: [strings]}), dependency_notes (string)."
        )
        prior = {
            "solution_design": ctx.stage_outputs.get("solution_design", {}),
            "solution_analysis": ctx.stage_outputs.get("solution_analysis", {}),
        }
        user_prompt = f"{self._base_context_str(ctx)}\n\nPrior analysis:\n{prior}"

        try:
            output = await self._call_with_tools(self._build_messages(system_prompt, user_prompt), session, ctx)
            return StageOutput(stage_name=self.stage_name, output=output)
        except Exception as e:
            return StageOutput(stage_name=self.stage_name, output={}, failed=True, failure_reason=str(e))
