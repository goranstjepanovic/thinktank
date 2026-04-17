import asyncio
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.engine import AsyncSessionLocal
from app.db.models import Idea, SolutionBranch
from app.events import schemas as ev
from app.events.bus import event_bus
from app.pipeline.context import BranchContext


class Orchestrator:
    def __init__(self) -> None:
        self._inference_client = None
        # idea_id → {branch_id → (task, context)}
        self._active: dict[str, dict[str, tuple[asyncio.Task, BranchContext]]] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._semaphore: asyncio.Semaphore | None = None
        self._queue_worker: asyncio.Task | None = None

    def setup(self, inference_client) -> None:
        """Called during app lifespan with the configured InferenceClient."""
        from app.pipeline.failure_analysis import FailureAnalyser
        self._inference_client = inference_client
        self._failure_analyser = FailureAnalyser(inference_client)
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_branches)
        self._queue_worker = asyncio.create_task(self._process_queue())

    async def start_idea(self, idea_id: str) -> None:
        """Spawn initial solution branches for a new idea."""
        async with AsyncSessionLocal() as session:
            idea = await self._get_idea(session, idea_id)
            if not idea:
                return
            idea.status = "RUNNING"
            await session.commit()

            # Generate distinct solution directions before spawning branches
            directions = await self._generate_initial_directions(idea, session)

            for i in range(1, settings.initial_branches_per_idea + 1):
                direction = directions[i - 1] if i - 1 < len(directions) else None
                branch = SolutionBranch(
                    idea_id=idea_id,
                    branch_index=i,
                    status="QUEUED",
                    current_stage=0,
                    approach_summary=direction,
                    inherited_context=json.dumps({"initial_direction": direction}) if direction else None,
                )
                session.add(branch)
                await session.commit()
                await session.refresh(branch)
                inherited = {"initial_direction": direction} if direction else {}
                await event_bus.publish(ev.branch_spawned(idea_id, branch.id, i, direction, None))
                await self._queue.put((idea_id, branch.id, i, None, inherited))

    async def _generate_initial_directions(self, idea: "Idea", session) -> list[str]:
        """Ask the model to propose N distinct solution directions before spawning branches."""
        from app.inference.base import Message

        n = settings.initial_branches_per_idea
        system_prompt = (
            f"You are a solution strategist. Your task is to propose {n} DISTINCT solution approaches "
            f"for the idea described below. Each approach must be fundamentally different in architecture "
            f"or technology strategy — not minor variations of the same approach. "
            f"CONSTRAINTS ARE ABSOLUTE: all approaches must respect every listed constraint. "
            f"Return JSON with key: directions (list of {n} strings, each 1-2 sentences summarising "
            f"the approach's unique angle and key technology choices)."
        )
        user_prompt = (
            f"Idea: {idea.name}\n"
            f"Description: {idea.description}\n"
            f"Requirements: {idea.requirements}\n"
            f"Constraints: {idea.constraints}\n\n"
            f"Propose {n} fundamentally different solution directions. "
            f"Ensure each direction clearly differs in its core architectural approach."
        )
        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt),
        ]
        try:
            output = await self._inference_client.call(
                stage_key="approach_generation",
                messages=messages,
                session=session,
                idea_id=idea.id,
                branch_id=None,
                call_type="FAILURE_ANALYSIS",  # reuse enum; no dedicated type needed
                call_index=0,
            )
            dirs = output.get("directions", [])
            if isinstance(dirs, list) and all(isinstance(d, str) for d in dirs):
                return dirs[:n]
        except Exception:
            pass  # fall back to null directions — pipeline still works
        return []

    async def on_branch_failed(self, idea_id: str, branch_id: str) -> None:
        """Called by BranchRunner when a branch fails. Triggers failure analysis."""
        if idea_id in self._active and branch_id in self._active[idea_id]:
            del self._active[idea_id][branch_id]

        # Hard cap: never exceed max_branches_per_idea total branches
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(SolutionBranch).where(SolutionBranch.idea_id == idea_id)
            )
            total_branches = len(result.scalars().all())

        if total_branches >= settings.max_branches_per_idea:
            await self._check_convergence(idea_id)
            return

        analysis = await self._failure_analyser.analyse(idea_id, branch_id)
        await event_bus.publish(ev.failure_analysis_completed(
            idea_id, branch_id,
            analysis.get("new_path_exists", False),
            None,
        ))

        if analysis.get("new_path_exists"):
            await self._spawn_branch(
                idea_id=idea_id,
                parent_branch_id=branch_id,
                suggested_direction=analysis.get("suggested_direction"),
            )
        else:
            await self._check_convergence(idea_id)

    async def on_branch_completed(self, idea_id: str, branch_id: str) -> None:
        """Called by BranchRunner when a branch reaches VIABLE."""
        if idea_id in self._active and branch_id in self._active[idea_id]:
            del self._active[idea_id][branch_id]
        await self._check_convergence(idea_id)

    async def pause_idea(self, idea_id: str) -> None:
        async with AsyncSessionLocal() as session:
            idea = await self._get_idea(session, idea_id)
            if idea:
                idea.status = "PAUSED"
                await session.commit()

        for branch_id, (task, ctx) in self._active.get(idea_id, {}).items():
            ctx.pause_event.clear()

    async def resume_idea(self, idea_id: str) -> None:
        async with AsyncSessionLocal() as session:
            idea = await self._get_idea(session, idea_id)
            if idea:
                idea.status = "RUNNING"
                await session.commit()

        for branch_id, (task, ctx) in self._active.get(idea_id, {}).items():
            ctx.pause_event.set()

    async def abandon_idea(self, idea_id: str) -> None:
        async with AsyncSessionLocal() as session:
            idea = await self._get_idea(session, idea_id)
            if idea:
                idea.status = "ABANDONED"
                await session.commit()

        for branch_id, (task, ctx) in list(self._active.get(idea_id, {}).items()):
            ctx.cancel_event.set()
            task.cancel()

        self._active.pop(idea_id, None)
        await event_bus.publish(ev.idea_abandoned(idea_id))

    async def _spawn_branch(self, idea_id: str, parent_branch_id: str, suggested_direction: str | None) -> None:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(SolutionBranch).where(SolutionBranch.idea_id == idea_id)
            )
            existing = result.scalars().all()
            next_index = max((b.branch_index for b in existing), default=0) + 1

            # Build inherited context from parent failure
            parent_result = await session.execute(
                select(SolutionBranch).where(SolutionBranch.id == parent_branch_id)
            )
            parent = parent_result.scalar_one_or_none()
            inherited = {
                "parent_branch_index": parent.branch_index if parent else None,
                "parent_failure_reason": parent.failure_reason if parent else None,
                "parent_failure_stage": parent.failure_stage if parent else None,
                "suggested_direction": suggested_direction,
            }

            branch = SolutionBranch(
                idea_id=idea_id,
                parent_branch_id=parent_branch_id,
                branch_index=next_index,
                status="QUEUED",
                current_stage=0,
                approach_summary=suggested_direction,
                inherited_context=json.dumps(inherited),
            )
            session.add(branch)
            await session.commit()
            await session.refresh(branch)

        await event_bus.publish(ev.branch_spawned(idea_id, branch.id, next_index, suggested_direction, parent_branch_id))
        await self._queue.put((idea_id, branch.id, next_index, parent_branch_id, inherited))

    async def _check_convergence(self, idea_id: str) -> None:
        """Mark idea as CONVERGED if no active branches remain."""
        if self._active.get(idea_id):
            return  # still have running branches

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(SolutionBranch).where(SolutionBranch.idea_id == idea_id)
            )
            branches = result.scalars().all()
            active = [b for b in branches if b.status in ("QUEUED", "RUNNING", "PAUSED")]
            viable = [b.id for b in branches if b.status == "VIABLE"]

            if not active:
                idea = await self._get_idea(session, idea_id)
                if idea and idea.status not in ("ABANDONED", "CONVERGED"):
                    idea.status = "CONVERGED"
                    await session.commit()
                await event_bus.publish(ev.idea_converged(idea_id, viable))

    async def _process_queue(self) -> None:
        """Background task that dequeues branch jobs and runs them within the semaphore limit."""
        from app.pipeline.runner import BranchRunner

        while True:
            idea_id, branch_id, branch_index, parent_branch_id, inherited_ctx = await self._queue.get()

            async def run_branch(iid, bid, bidx, parent, inherited):
                async with self._semaphore:
                    ctx = BranchContext(
                        idea_id=iid,
                        branch_id=bid,
                        branch_index=bidx,
                        inherited_context=inherited or {},
                    )
                    runner = BranchRunner(self._inference_client)
                    task = asyncio.create_task(runner.run(ctx))
                    self._active.setdefault(iid, {})[bid] = (task, ctx)
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    finally:
                        self._active.get(iid, {}).pop(bid, None)

            asyncio.create_task(run_branch(idea_id, branch_id, branch_index, parent_branch_id, inherited_ctx))

    async def _get_idea(self, session: AsyncSession, idea_id: str) -> Idea | None:
        result = await session.execute(select(Idea).where(Idea.id == idea_id))
        return result.scalar_one_or_none()


# Singleton — imported by API routes and runner
orchestrator = Orchestrator()
