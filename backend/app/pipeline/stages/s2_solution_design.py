from app.pipeline.context import BranchContext
from app.pipeline.stages.base_stage import BaseStage, StageOutput


class SolutionDesignStage(BaseStage):
    stage_key = "solution_design"
    stage_index = 2
    stage_name = "solution_design"

    async def run(self, ctx: BranchContext, session) -> StageOutput:
        direction = (ctx.inherited_context or {}).get("initial_direction") or (ctx.inherited_context or {}).get("suggested_direction")
        direction_instruction = (
            f" You MUST follow this assigned solution direction: {direction}"
            if direction else
            " This branch represents one specific solution path — make it distinct and specific."
        )
        system_prompt = (
            f"You are a solution architect. Design a concrete, detailed solution approach for the idea.{direction_instruction} "
            "Return JSON with keys: solution_name (string), approach (string), "
            "key_technologies (list), architecture_summary (string), "
            "major_components (list of {name, purpose}), risks (list), reasoning (string)."
        )
        prior = {
            "intake": ctx.stage_outputs.get("intake", {}),
            "feasibility": ctx.stage_outputs.get("feasibility_scan", {}),
            "inherited_context": ctx.inherited_context,
        }
        user_prompt = f"{self._base_context_str(ctx)}\n\nPrior analysis:\n{prior}"

        try:
            output = await self._call_with_tools(self._build_messages(system_prompt, user_prompt), session, ctx)
            return StageOutput(stage_name=self.stage_name, output=output)
        except Exception as e:
            return StageOutput(stage_name=self.stage_name, output={}, failed=True, failure_reason=str(e))
