from app.pipeline.context import BranchContext
from app.pipeline.stages.base_stage import BaseStage, StageOutput


class SolutionAnalysisStage(BaseStage):
    stage_key = "solution_analysis"
    stage_index = 3
    stage_name = "solution_analysis"

    async def run(self, ctx: BranchContext, session) -> StageOutput:
        system_prompt = (
            "You are a solution evaluator. Critically assess the proposed solution against the "
            "original requirements and constraints. Identify gaps, risks, and feasibility concerns. "
            "Return JSON with keys: verdict (PASS|FAIL), confidence (0.0-1.0), "
            "requirements_coverage (list of {requirement, covered: bool, notes}), "
            "risks (list of {risk, likelihood: low|medium|high, impact: low|medium|high}), "
            "gaps (list), reasoning (string)."
        )
        prior = {
            "intake": ctx.stage_outputs.get("intake", {}),
            "feasibility": ctx.stage_outputs.get("feasibility_scan", {}),
            "solution_design": ctx.stage_outputs.get("solution_design", {}),
        }
        user_prompt = f"{self._base_context_str(ctx)}\n\nPrior analysis:\n{prior}"

        try:
            output = await self._call_with_tools(self._build_messages(system_prompt, user_prompt), session, ctx)
            failed = output.get("verdict") == "FAIL"
            reason = output.get("reasoning") if failed else None
            return StageOutput(stage_name=self.stage_name, output=output, failed=failed, failure_reason=reason)
        except Exception as e:
            return StageOutput(stage_name=self.stage_name, output={}, failed=True, failure_reason=str(e))
