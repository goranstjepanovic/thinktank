import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import AsyncSessionLocal
from app.db.models import FailureAnalysis, SolutionBranch
from app.inference.base import Message
from app.inference.client import InferenceClient


class FailureAnalyser:
    def __init__(self, inference_client: InferenceClient) -> None:
        self._client = inference_client

    async def analyse(self, idea_id: str, failed_branch_id: str) -> dict:
        """
        Given a failed branch, determine if a new unexplored solution path exists.
        Returns: { new_path_exists: bool, suggested_direction: str|None, reasoning: str }
        """
        async with AsyncSessionLocal() as session:
            # Gather failed branch details
            result = await session.execute(
                select(SolutionBranch).where(SolutionBranch.id == failed_branch_id)
            )
            failed_branch = result.scalar_one_or_none()
            if not failed_branch:
                return {"new_path_exists": False, "suggested_direction": None, "reasoning": "Branch not found"}

            # Gather all previously explored branches for this idea
            result = await session.execute(
                select(SolutionBranch).where(SolutionBranch.idea_id == idea_id)
            )
            all_branches = result.scalars().all()

            explored_paths = [
                {
                    "branch_index": b.branch_index,
                    "approach_summary": b.approach_summary,
                    "status": b.status,
                    "failure_stage": b.failure_stage,
                    "failure_reason": b.failure_reason,
                }
                for b in all_branches
            ]

            # Gather original idea constraints
            from app.db.models import Idea
            idea_result = await session.execute(
                select(Idea).where(Idea.id == idea_id)
            )
            idea = idea_result.scalar_one_or_none()
            constraints_text = idea.constraints if idea else ""

            system_prompt = (
                "You are a solution space analyst. A solution branch has failed. "
                "Given the failure details and all previously explored paths, determine if a "
                "meaningfully different solution approach exists that has NOT been tried yet "
                "AND that respects ALL of the idea's hard constraints. "
                "IMPORTANT: Do NOT suggest paths that violate any constraint — these will fail "
                "immediately and waste compute. Only return new_path_exists=true if you can "
                "identify a genuinely novel approach that stays within the stated constraints. "
                "If all viable approaches within the constraints have been tried, return false. "
                "Return JSON with keys: new_path_exists (bool), "
                "suggested_direction (string or null — specific unexplored angle that respects all constraints), "
                "reasoning (string)."
            )
            user_prompt = (
                f"Hard constraints that ALL solutions must satisfy:\n{constraints_text}\n\n"
                f"Failed branch details:\n"
                f"  Approach: {failed_branch.approach_summary}\n"
                f"  Failed at stage: {failed_branch.failure_stage}\n"
                f"  Reason: {failed_branch.failure_reason}\n\n"
                f"All explored paths so far:\n{json.dumps(explored_paths, indent=2)}"
            )

            analysis_result = await self._client.call(
                stage_key="failure_analysis",
                messages=[
                    Message(role="system", content=system_prompt),
                    Message(role="user", content=user_prompt),
                ],
                session=session,
                idea_id=idea_id,
                branch_id=None,
                stage_result_id=None,
                call_type="FAILURE_ANALYSIS",
            )

            # Persist the analysis
            fa = FailureAnalysis(
                idea_id=idea_id,
                failed_branch_id=failed_branch_id,
                new_path_exists=analysis_result.get("new_path_exists", False),
                suggested_direction=analysis_result.get("suggested_direction"),
                reasoning=analysis_result.get("reasoning", ""),
            )
            session.add(fa)
            await session.commit()

            return analysis_result
