from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.inference.base import Message
from app.inference.client import InferenceClient
from app.pipeline.context import BranchContext


@dataclass
class StageOutput:
    stage_name: str
    output: dict
    failed: bool = False
    failure_reason: str | None = None


class StageError(Exception):
    pass


class BaseStage(ABC):
    stage_key: str  # must match a key in models.yaml stages
    stage_index: int
    stage_name: str

    def __init__(self, inference_client: InferenceClient) -> None:
        self._client = inference_client

    @abstractmethod
    async def run(self, ctx: BranchContext, session) -> StageOutput:
        """Execute this stage. Return StageOutput — never raise for model failures."""
        ...

    # ------------------------------------------------------------------
    # Convenience wrappers — stages should use these instead of calling
    # self._client.call() directly so that stage_result_id is always set.
    # ------------------------------------------------------------------

    async def _call(
        self,
        messages: list[Message],
        session,
        ctx: BranchContext,
        call_index: int = 0,
        overrides: dict | None = None,
    ) -> dict:
        """Single-turn call wired to the current stage and branch context."""
        return await self._client.call(
            stage_key=self.stage_key,
            messages=messages,
            session=session,
            idea_id=ctx.idea_id,
            branch_id=ctx.branch_id,
            stage_result_id=ctx.current_stage_result_id,
            call_index=call_index,
            overrides=overrides,
        )

    async def _call_with_tools(
        self,
        messages: list[Message],
        session,
        ctx: BranchContext,
        call_index: int = 0,
    ) -> dict:
        """
        Multi-turn call with the Python Script Runner tool available.
        The model may invoke `run_python` zero or more times before returning JSON.
        """
        return await self._client.call_with_tools(
            stage_key=self.stage_key,
            messages=messages,
            session=session,
            idea_id=ctx.idea_id,
            branch_id=ctx.branch_id,
            stage_result_id=ctx.current_stage_result_id,
            call_index=call_index,
        )

    # ------------------------------------------------------------------
    # Prompt helpers
    # ------------------------------------------------------------------

    def _build_messages(self, system_prompt: str, user_prompt: str) -> list[Message]:
        return [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt),
        ]

    def _base_context_str(self, ctx: BranchContext) -> str:
        lines = [
            f"Idea: {ctx.idea_name}",
            f"Description: {ctx.idea_description}",
            f"Requirements: {ctx.idea_requirements}",
            f"Constraints: {ctx.idea_constraints}",
        ]
        if ctx.inherited_context:
            if "initial_direction" in ctx.inherited_context and ctx.inherited_context["initial_direction"]:
                lines.append(
                    f"\nAssigned solution direction for this branch: "
                    f"{ctx.inherited_context['initial_direction']}"
                )
            else:
                lines.append(f"\nInherited context from prior failed branch: {ctx.inherited_context}")
        return "\n".join(lines)
