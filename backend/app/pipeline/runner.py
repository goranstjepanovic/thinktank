import asyncio
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import AsyncSessionLocal
from app.db.models import Idea, SolutionBranch, StageResult
from app.events import schemas as ev
from app.events.bus import event_bus
from app.pipeline.context import BranchContext
from app.pipeline.stages.base_stage import BaseStage, StageOutput
from app.pipeline.stages.s0_intake import IntakeStage
from app.pipeline.stages.s1_feasibility import FeasibilityStage
from app.pipeline.stages.s2_solution_design import SolutionDesignStage
from app.pipeline.stages.s3_solution_analysis import SolutionAnalysisStage
from app.pipeline.stages.s4_decomposition import DecompositionStage
from app.pipeline.stages.s5_component_val import ComponentValidationStage
from app.pipeline.stages.s6_deep_review import DeepReviewStage
from app.pipeline.stages.s7_documentation import DocumentationStage
from sqlalchemy import select


def build_stages(inference_client) -> list[BaseStage]:
    return [
        IntakeStage(inference_client),
        FeasibilityStage(inference_client),
        SolutionDesignStage(inference_client),
        SolutionAnalysisStage(inference_client),
        DecompositionStage(inference_client),
        ComponentValidationStage(inference_client),
        DeepReviewStage(inference_client),
        DocumentationStage(inference_client),
    ]


class BranchRunner:
    def __init__(self, inference_client) -> None:
        self._stages = build_stages(inference_client)

    async def run(self, ctx: BranchContext) -> None:
        """
        Execute the stage pipeline for one solution branch.
        Notifies orchestrator on completion or failure via callback.
        """
        async with AsyncSessionLocal() as session:
            try:
                await self._run_pipeline(ctx, session)
            except asyncio.CancelledError:
                await self._mark_branch(session, ctx.branch_id, "CANCELLED")
                raise

    async def _run_pipeline(self, ctx: BranchContext, session: AsyncSession) -> None:
        await self._mark_idea_running(session, ctx.idea_id)
        await self._mark_branch(session, ctx.branch_id, "RUNNING")
        await event_bus.publish(ev.branch_started(ctx.idea_id, ctx.branch_id, ctx.branch_index))

        # Load idea context
        result = await session.execute(select(Idea).where(Idea.id == ctx.idea_id))
        idea = result.scalar_one()
        ctx.idea_name = idea.name
        ctx.idea_description = idea.description
        ctx.idea_requirements = idea.requirements
        ctx.idea_constraints = idea.constraints

        # Determine which stage to start from (resume support)
        start_stage = await self._get_resume_stage(session, ctx.branch_id)

        for stage in self._stages[start_stage:]:
            # Check pause between stages
            if not ctx.pause_event.is_set():
                await self._mark_branch(session, ctx.branch_id, "PAUSED")
                await event_bus.publish(ev.branch_paused(ctx.idea_id, ctx.branch_id, stage.stage_index))
                await ctx.pause_event.wait()
                await self._mark_branch(session, ctx.branch_id, "RUNNING")
                await event_bus.publish(ev.branch_resumed(ctx.idea_id, ctx.branch_id, stage.stage_index))

            # Check cancel
            if ctx.cancel_event.is_set():
                await self._mark_branch(session, ctx.branch_id, "CANCELLED")
                return

            stage_result_id = await self._mark_stage(session, ctx.branch_id, stage.stage_index, stage.stage_name, "RUNNING")
            ctx.current_stage_result_id = stage_result_id
            await event_bus.publish(ev.stage_started(ctx.idea_id, ctx.branch_id, stage.stage_index, stage.stage_name))
            await self._mark_branch_stage(session, ctx.branch_id, stage.stage_index)

            logger.info("Branch %s | stage %d (%s) starting", ctx.branch_id[:8], stage.stage_index, stage.stage_name)
            start_time = time.monotonic()
            output: StageOutput = await stage.run(ctx, session)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            if output.failed:
                logger.warning(
                    "Branch %s | stage %d (%s) FAILED in %.1fs: %s",
                    ctx.branch_id[:8], stage.stage_index, stage.stage_name,
                    duration_ms / 1000, output.failure_reason,
                )
                await self._mark_stage(session, ctx.branch_id, stage.stage_index, stage.stage_name, "FAILED",
                                       failed=True, failure_reason=output.failure_reason)
                await self._mark_branch(session, ctx.branch_id, "FAILED",
                                        failure_stage=stage.stage_index, failure_reason=output.failure_reason)
                await event_bus.publish(ev.stage_failed(ctx.idea_id, ctx.branch_id, stage.stage_index, output.failure_reason or ""))
                await event_bus.publish(ev.branch_failed(ctx.idea_id, ctx.branch_id, stage.stage_index, output.failure_reason or ""))

                # Notify orchestrator to run failure analysis
                from app.pipeline.orchestrator import orchestrator
                await orchestrator.on_branch_failed(ctx.idea_id, ctx.branch_id)
                return

            # Persist output and accumulate context
            import json
            logger.info(
                "Branch %s | stage %d (%s) completed in %.1fs",
                ctx.branch_id[:8], stage.stage_index, stage.stage_name, duration_ms / 1000,
            )
            await self._mark_stage(session, ctx.branch_id, stage.stage_index, stage.stage_name, "COMPLETED",
                                   output_json=json.dumps(output.output))
            ctx.stage_outputs[stage.stage_name] = output.output
            await event_bus.publish(ev.stage_completed(ctx.idea_id, ctx.branch_id, stage.stage_index, stage.stage_name, duration_ms))

        # All stages passed — mark VIABLE
        await self._mark_branch(session, ctx.branch_id, "VIABLE")
        await event_bus.publish(ev.branch_viable(ctx.idea_id, ctx.branch_id))

        from app.pipeline.orchestrator import orchestrator
        await orchestrator.on_branch_completed(ctx.idea_id, ctx.branch_id)

    async def _get_resume_stage(self, session: AsyncSession, branch_id: str) -> int:
        result = await session.execute(
            select(StageResult)
            .where(StageResult.branch_id == branch_id, StageResult.status == "COMPLETED")
        )
        completed = result.scalars().all()
        if not completed:
            return 0
        return max(s.stage_index for s in completed) + 1

    async def _mark_idea_running(self, session: AsyncSession, idea_id: str) -> None:
        result = await session.execute(select(Idea).where(Idea.id == idea_id))
        idea = result.scalar_one_or_none()
        if idea and idea.status == "QUEUED":
            idea.status = "RUNNING"
            await session.commit()

    async def _mark_branch(self, session: AsyncSession, branch_id: str, status: str,
                           failure_stage: int | None = None, failure_reason: str | None = None) -> None:
        result = await session.execute(select(SolutionBranch).where(SolutionBranch.id == branch_id))
        branch = result.scalar_one_or_none()
        if branch:
            branch.status = status
            if failure_stage is not None:
                branch.failure_stage = failure_stage
            if failure_reason is not None:
                branch.failure_reason = failure_reason
            await session.commit()

    async def _mark_branch_stage(self, session: AsyncSession, branch_id: str, stage_index: int) -> None:
        result = await session.execute(select(SolutionBranch).where(SolutionBranch.id == branch_id))
        branch = result.scalar_one_or_none()
        if branch:
            branch.current_stage = stage_index
            await session.commit()

    async def _mark_stage(self, session: AsyncSession, branch_id: str, stage_index: int, stage_name: str,
                          status: str, failed: bool = False, failure_reason: str | None = None,
                          output_json: str | None = None) -> str:
        """Upsert a StageResult row and return its ID."""
        result = await session.execute(
            select(StageResult).where(StageResult.branch_id == branch_id, StageResult.stage_index == stage_index)
        )
        stage = result.scalar_one_or_none()
        now = datetime.now(timezone.utc)

        if stage is None:
            stage = StageResult(
                branch_id=branch_id,
                stage_index=stage_index,
                stage_name=stage_name,
                status=status,
                started_at=now if status == "RUNNING" else None,
            )
            session.add(stage)
        else:
            stage.status = status
            if status == "RUNNING":
                stage.started_at = now
            elif status in ("COMPLETED", "FAILED"):
                stage.completed_at = now

        if failed:
            stage.failed = True
            stage.failure_reason = failure_reason
        if output_json:
            stage.output_json = output_json

        await session.commit()
        return stage.id
