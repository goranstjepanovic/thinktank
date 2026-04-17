from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.engine import get_session
from app.db.models import SolutionBranch
from app.schemas.branch import BranchDetailResponse, StageResultResponse

router = APIRouter(prefix="/ideas/{idea_id}/branches", tags=["branches"])


@router.get("", response_model=list[BranchDetailResponse])
async def list_branches(idea_id: str, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(SolutionBranch)
        .where(SolutionBranch.idea_id == idea_id)
        .options(selectinload(SolutionBranch.stage_results))
        .order_by(SolutionBranch.branch_index)
    )
    branches = result.scalars().all()
    return [_to_detail(b) for b in branches]


@router.get("/{branch_id}", response_model=BranchDetailResponse)
async def get_branch(idea_id: str, branch_id: str, session: AsyncSession = Depends(get_session)):
    branch = await _get_or_404(idea_id, branch_id, session)
    return _to_detail(branch)


@router.get("/{branch_id}/stages", response_model=list[StageResultResponse])
async def list_stages(idea_id: str, branch_id: str, session: AsyncSession = Depends(get_session)):
    branch = await _get_or_404(idea_id, branch_id, session)
    return [StageResultResponse.model_validate(s) for s in branch.stage_results]


@router.get("/{branch_id}/stages/{stage_index}", response_model=StageResultResponse)
async def get_stage(idea_id: str, branch_id: str, stage_index: int, session: AsyncSession = Depends(get_session)):
    branch = await _get_or_404(idea_id, branch_id, session)
    stage = next((s for s in branch.stage_results if s.stage_index == stage_index), None)
    if not stage:
        raise HTTPException(status_code=404, detail="Stage not found")
    return StageResultResponse.model_validate(stage)


async def _get_or_404(idea_id: str, branch_id: str, session: AsyncSession) -> SolutionBranch:
    result = await session.execute(
        select(SolutionBranch)
        .where(SolutionBranch.id == branch_id, SolutionBranch.idea_id == idea_id)
        .options(selectinload(SolutionBranch.stage_results))
    )
    branch = result.scalar_one_or_none()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    return branch


def _to_detail(branch: SolutionBranch) -> BranchDetailResponse:
    stages = sorted(branch.stage_results, key=lambda s: s.stage_index)
    return BranchDetailResponse(
        id=branch.id,
        idea_id=branch.idea_id,
        branch_index=branch.branch_index,
        status=branch.status,
        current_stage=branch.current_stage,
        approach_summary=branch.approach_summary,
        parent_branch_id=branch.parent_branch_id,
        failure_stage=branch.failure_stage,
        failure_reason=branch.failure_reason,
        created_at=branch.created_at,
        updated_at=branch.updated_at,
        stage_results=[StageResultResponse.model_validate(s) for s in stages],
    )
