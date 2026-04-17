from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models import ModelCall

router = APIRouter(prefix="/ideas/{idea_id}/model-calls", tags=["audit"])


class ModelCallResponse(BaseModel):
    id: str
    branch_id: str | None
    stage_result_id: str | None
    call_type: str
    call_index: int
    model_name: str
    backend: str
    prompt_json: str
    response_json: str
    tokens_prompt: int | None
    tokens_completion: int | None
    duration_ms: int | None
    created_at: str

    model_config = {"from_attributes": True}


@router.get("", response_model=list[ModelCallResponse])
async def list_model_calls(
    idea_id: str,
    branch_id: str | None = None,
    call_type: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    q = select(ModelCall).where(ModelCall.idea_id == idea_id)
    if branch_id:
        q = q.where(ModelCall.branch_id == branch_id)
    if call_type:
        q = q.where(ModelCall.call_type == call_type.upper())
    q = q.order_by(ModelCall.created_at)
    result = await session.execute(q)
    calls = result.scalars().all()
    return [ModelCallResponse(
        id=c.id, branch_id=c.branch_id, stage_result_id=c.stage_result_id,
        call_type=c.call_type, call_index=c.call_index, model_name=c.model_name,
        backend=c.backend, prompt_json=c.prompt_json, response_json=c.response_json,
        tokens_prompt=c.tokens_prompt, tokens_completion=c.tokens_completion,
        duration_ms=c.duration_ms, created_at=c.created_at.isoformat(),
    ) for c in calls]
