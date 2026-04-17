from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models import FailureAnalysis

router = APIRouter(prefix="/ideas/{idea_id}/failure-analyses", tags=["audit"])


class FailureAnalysisResponse(BaseModel):
    id: str
    failed_branch_id: str
    new_path_exists: bool
    suggested_direction: str | None
    reasoning: str
    spawned_branch_id: str | None
    created_at: str


@router.get("", response_model=list[FailureAnalysisResponse])
async def list_failure_analyses(idea_id: str, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(FailureAnalysis)
        .where(FailureAnalysis.idea_id == idea_id)
        .order_by(FailureAnalysis.created_at)
    )
    analyses = result.scalars().all()
    return [FailureAnalysisResponse(
        id=a.id, failed_branch_id=a.failed_branch_id, new_path_exists=a.new_path_exists,
        suggested_direction=a.suggested_direction, reasoning=a.reasoning,
        spawned_branch_id=a.spawned_branch_id, created_at=a.created_at.isoformat(),
    ) for a in analyses]
