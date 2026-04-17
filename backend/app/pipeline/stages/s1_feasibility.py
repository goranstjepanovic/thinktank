from app.pipeline.context import BranchContext
from app.pipeline.stages.base_stage import BaseStage, StageOutput


class FeasibilityStage(BaseStage):
    stage_key = "feasibility_scan"
    stage_index = 1
    stage_name = "feasibility_scan"

    async def run(self, ctx: BranchContext, session) -> StageOutput:
        system_prompt = (
            "You are a technical feasibility analyst. Evaluate whether the described idea is "
            "achievable given the stated constraints. CONSTRAINTS ARE HARD REQUIREMENTS — "
            "if the proposed solution violates ANY constraint, verdict MUST be FAIL regardless "
            "of other merits. Be rigorous — a false PASS wastes all downstream analysis. "
            "Return JSON with keys: verdict (PASS|FAIL), confidence (0.0-1.0), "
            "blockers (list of strings — include constraint violations here, empty if PASS), "
            "warnings (list of strings), reasoning (string)."
        )
        intake = ctx.stage_outputs.get("intake", {})
        user_prompt = (
            f"{self._base_context_str(ctx)}\n\n"
            f"Parsed analysis from intake stage:\n{intake}"
        )

        try:
            output = await self._call_with_tools(self._build_messages(system_prompt, user_prompt), session, ctx)
            failed = output.get("verdict") == "FAIL"
            reason = "; ".join(output.get("blockers", [])) if failed else None
            return StageOutput(stage_name=self.stage_name, output=output, failed=failed, failure_reason=reason)
        except Exception as e:
            return StageOutput(stage_name=self.stage_name, output={}, failed=True, failure_reason=str(e))
