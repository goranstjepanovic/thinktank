from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.engine import get_session
from app.db.models import Idea, Phase2Session, Phase3Session, SolutionBranch
from app.events import schemas as ev
from app.events.bus import event_bus
from app.schemas.idea import IdeaCreate, IdeaDetailResponse, IdeaSummaryResponse

router = APIRouter(prefix="/ideas", tags=["ideas"])


@router.post("", response_model=IdeaDetailResponse, status_code=201)
async def create_idea(body: IdeaCreate, session: AsyncSession = Depends(get_session)):
    from app.pipeline.orchestrator import orchestrator

    idea = Idea(
        name=body.name,
        description=body.description,
        requirements=body.requirements,
        constraints=body.constraints,
        status="QUEUED",
        parent_idea_id=body.parent_idea_id,
    )
    session.add(idea)
    await session.commit()
    await session.refresh(idea)

    # Kick off background analysis
    await orchestrator.start_idea(idea.id)

    await session.refresh(idea, ["branches"])
    return _to_detail(idea)


@router.get("", response_model=list[IdeaSummaryResponse])
async def list_ideas(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Idea).options(selectinload(Idea.branches)).order_by(Idea.created_at.desc())
    )
    ideas = result.scalars().all()

    p2_result = await session.execute(select(Phase2Session.idea_id, Phase2Session.status))
    p2_map: dict[str, str] = {row.idea_id: row.status for row in p2_result}

    p3_result = await session.execute(select(Phase3Session.idea_id, Phase3Session.status))
    p3_map: dict[str, str] = {row.idea_id: row.status for row in p3_result}

    idea_names: dict[str, str] = {i.id: i.name for i in ideas}

    return [_to_summary(i, p2_map.get(i.id), p3_map.get(i.id), idea_names.get(i.parent_idea_id) if i.parent_idea_id else None) for i in ideas]


@router.get("/{idea_id}", response_model=IdeaDetailResponse)
async def get_idea(idea_id: str, session: AsyncSession = Depends(get_session)):
    idea = await _get_or_404(idea_id, session)
    return _to_detail(idea)


@router.post("/{idea_id}/pause")
async def pause_idea(idea_id: str, session: AsyncSession = Depends(get_session)):
    from app.pipeline.orchestrator import orchestrator

    idea = await _get_or_404(idea_id, session)
    if idea.status != "RUNNING":
        raise HTTPException(status_code=409, detail=f"Idea is {idea.status}, not RUNNING")
    await orchestrator.pause_idea(idea_id)
    return {"status": "PAUSED"}


@router.post("/{idea_id}/resume")
async def resume_idea(idea_id: str, session: AsyncSession = Depends(get_session)):
    from app.pipeline.orchestrator import orchestrator

    idea = await _get_or_404(idea_id, session)
    if idea.status != "PAUSED":
        raise HTTPException(status_code=409, detail=f"Idea is {idea.status}, not PAUSED")
    await orchestrator.resume_idea(idea_id)
    return {"status": "RUNNING"}


@router.post("/{idea_id}/abandon")
async def abandon_idea(idea_id: str, session: AsyncSession = Depends(get_session)):
    from app.pipeline.orchestrator import orchestrator

    idea = await _get_or_404(idea_id, session)
    if idea.status in ("CONVERGED", "ABANDONED"):
        raise HTTPException(status_code=409, detail=f"Idea is already {idea.status}")
    await orchestrator.abandon_idea(idea_id)
    return {"status": "ABANDONED"}


class SelectionBody(BaseModel):
    notes: str = ""


@router.post("/{idea_id}/select/{branch_id}", response_model=IdeaDetailResponse)
async def select_solution(
    idea_id: str,
    branch_id: str,
    body: SelectionBody = SelectionBody(),
    session: AsyncSession = Depends(get_session),
):
    idea = await _get_or_404(idea_id, session)
    if idea.status not in ("CONVERGED", "SELECTED"):
        raise HTTPException(status_code=409, detail=f"Idea is {idea.status}; can only select from CONVERGED ideas")

    # Verify the branch exists, belongs to this idea, and is VIABLE
    result = await session.execute(
        select(SolutionBranch).where(SolutionBranch.id == branch_id, SolutionBranch.idea_id == idea_id)
    )
    branch = result.scalar_one_or_none()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    if branch.status != "VIABLE":
        raise HTTPException(status_code=409, detail=f"Branch is {branch.status}, not VIABLE")

    # If switching to a different branch, delete stale Phase 2 and Phase 3 sessions.
    if idea.selected_branch_id and idea.selected_branch_id != branch_id:
        from app.db.models import Phase2Session, Phase3Session
        p2_result = await session.execute(
            select(Phase2Session).where(Phase2Session.idea_id == idea_id)
        )
        stale_p2 = p2_result.scalar_one_or_none()
        if stale_p2:
            await session.delete(stale_p2)
        p3_result = await session.execute(
            select(Phase3Session).where(Phase3Session.idea_id == idea_id)
        )
        stale_p3 = p3_result.scalar_one_or_none()
        if stale_p3:
            await session.delete(stale_p3)

    idea.selected_branch_id = branch_id
    idea.selected_at = datetime.now(timezone.utc)
    idea.selection_notes = body.notes or None
    idea.status = "SELECTED"
    await session.commit()
    await session.refresh(idea, ["branches"])

    await event_bus.publish(ev.idea_selected(idea_id, branch_id))
    return _to_detail(idea)


@router.delete("/{idea_id}", status_code=204)
async def delete_idea(idea_id: str, session: AsyncSession = Depends(get_session)):
    from app.pipeline.orchestrator import orchestrator

    idea = await _get_or_404(idea_id, session)
    # Only abandon if the idea is still active — orchestrator has nothing to cancel otherwise
    if idea.status in ("RUNNING", "PAUSED", "QUEUED"):
        await orchestrator.abandon_idea(idea_id)
    await session.delete(idea)
    await session.commit()


# --- helpers ---

async def _get_or_404(idea_id: str, session: AsyncSession) -> Idea:
    result = await session.execute(
        select(Idea).options(selectinload(Idea.branches)).where(Idea.id == idea_id)
    )
    idea = result.scalar_one_or_none()
    if not idea:
        raise HTTPException(status_code=404, detail="Idea not found")
    return idea


_P3_LABELS = {
    "PLANNING": "Phase 3 · Planning",
    "RUNNING": "Phase 3 · Building",
    "WAITING": "Phase 3 · Waiting",
    "COMPLETE": "Phase 3 · Complete",
    "FAILED": "Phase 3 · Failed",
}
_P2_LABELS = {
    "RESOLVING": "Phase 2 · Clarifying",
    "READY": "Phase 2 · Ready",
    "IMPLEMENTING": "Phase 2 · Implementing",
    "COMPLETE": "Phase 2 · Complete",
}
_P1_LABELS = {
    "QUEUED": "Queued",
    "RUNNING": "Running",
    "PAUSED": "Paused",
    "CONVERGED": "Converged",
    "ABANDONED": "Abandoned",
    "SELECTED": "Selected",
}


def _to_summary(
    idea: Idea,
    p2_status: str | None = None,
    p3_status: str | None = None,
    parent_idea_name: str | None = None,
) -> IdeaSummaryResponse:
    active = sum(1 for b in idea.branches if b.status in ("QUEUED", "RUNNING", "PAUSED"))
    viable = sum(1 for b in idea.branches if b.status == "VIABLE")

    if p3_status:
        phase, phase_label = 3, _P3_LABELS.get(p3_status, f"Phase 3 · {p3_status}")
    elif p2_status:
        phase, phase_label = 2, _P2_LABELS.get(p2_status, f"Phase 2 · {p2_status}")
    else:
        phase, phase_label = 1, _P1_LABELS.get(idea.status, idea.status.title())

    return IdeaSummaryResponse(
        id=idea.id,
        name=idea.name,
        status=idea.status,
        active_branch_count=active,
        viable_branch_count=viable,
        phase=phase,
        phase_label=phase_label,
        parent_idea_id=idea.parent_idea_id,
        parent_idea_name=parent_idea_name,
        created_at=idea.created_at,
        updated_at=idea.updated_at,
    )


def _to_detail(idea: Idea) -> IdeaDetailResponse:
    from app.schemas.idea import BranchSummary
    branches = [
        BranchSummary(
            id=b.id,
            branch_index=b.branch_index,
            status=b.status,
            current_stage=b.current_stage,
            approach_summary=b.approach_summary,
            parent_branch_id=b.parent_branch_id,
            failure_reason=b.failure_reason,
            created_at=b.created_at,
            updated_at=b.updated_at,
        )
        for b in idea.branches
    ]
    return IdeaDetailResponse(
        id=idea.id,
        name=idea.name,
        description=idea.description,
        requirements=idea.requirements,
        constraints=idea.constraints,
        status=idea.status,
        selected_branch_id=idea.selected_branch_id,
        selected_at=idea.selected_at,
        selection_notes=idea.selection_notes,
        created_at=idea.created_at,
        updated_at=idea.updated_at,
        branches=branches,
    )
