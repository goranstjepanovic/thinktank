from app.pipeline.context import BranchContext
from app.pipeline.stages.base_stage import BaseStage, StageOutput


class DeepReviewStage(BaseStage):
    stage_key = "deep_review"
    stage_index = 6
    stage_name = "deep_review"

    async def run(self, ctx: BranchContext, session) -> StageOutput:
        system_prompt = (
            "You are a senior technical reviewer performing a final holistic assessment. "
            "Review ALL prior analysis and determine if this solution is truly viable for implementation. "
            "Return JSON with keys: verdict (VIABLE|FAILED), confidence (0.0-1.0), "
            "gaps (list), conflicts (list), unmet_requirements (list), "
            "recommendation (string), reasoning (string)."
        )
        prior = {k: v for k, v in ctx.stage_outputs.items()}
        user_prompt = f"{self._base_context_str(ctx)}\n\nComplete prior analysis:\n{prior}"

        try:
            output = await self._call_with_tools(self._build_messages(system_prompt, user_prompt), session, ctx)
            failed = output.get("verdict") == "FAILED"
            reason = output.get("reasoning") if failed else None
            return StageOutput(stage_name=self.stage_name, output=output, failed=failed, failure_reason=reason)
        except Exception as e:
            return StageOutput(stage_name=self.stage_name, output={}, failed=True, failure_reason=str(e))
