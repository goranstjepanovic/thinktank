from app.pipeline.context import BranchContext
from app.pipeline.stages.base_stage import BaseStage, StageOutput


class ComponentValidationStage(BaseStage):
    stage_key = "component_validator"
    stage_index = 5
    stage_name = "component_validation"

    async def run(self, ctx: BranchContext, session) -> StageOutput:
        decomp = ctx.stage_outputs.get("decomposition", {})
        components = decomp.get("components", [])

        if not components:
            return StageOutput(
                stage_name=self.stage_name, output={}, failed=True,
                failure_reason="No components found from decomposition stage"
            )

        system_prompt = (
            "You are a component validator. Evaluate a single component for technical feasibility. "
            "Return JSON with keys: component_id (string), verdict (VALID|RISKY|UNACHIEVABLE), "
            "confidence (0.0-1.0), issues (list), mitigations (list), reasoning (string)."
        )

        results = []
        any_unachievable = False

        for i, component in enumerate(components):
            user_prompt = (
                f"{self._base_context_str(ctx)}\n\n"
                f"Full solution context:\n{ctx.stage_outputs.get('solution_design', {})}\n\n"
                f"Component to validate:\n{component}"
            )
            try:
                result = await self._call_with_tools(
                    self._build_messages(system_prompt, user_prompt), session, ctx, call_index=i
                )
                results.append(result)
                if result.get("verdict") == "UNACHIEVABLE":
                    any_unachievable = True
            except Exception as e:
                results.append({"component_id": component.get("id", str(i)), "error": str(e)})

        output = {"component_results": results}
        if any_unachievable:
            unachievable = [r for r in results if r.get("verdict") == "UNACHIEVABLE"]
            reason = "; ".join(
                f"{r.get('component_id')}: {r.get('reasoning', '')}" for r in unachievable
            )
            return StageOutput(stage_name=self.stage_name, output=output, failed=True, failure_reason=reason)

        return StageOutput(stage_name=self.stage_name, output=output)
