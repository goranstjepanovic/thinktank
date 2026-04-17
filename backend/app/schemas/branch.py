from datetime import datetime

from pydantic import BaseModel


class StageResultResponse(BaseModel):
    id: str
    stage_index: int
    stage_name: str
    status: str
    output_json: str | None
    failed: bool
    failure_reason: str | None
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class BranchDetailResponse(BaseModel):
    id: str
    idea_id: str
    branch_index: int
    status: str
    current_stage: int
    approach_summary: str | None
    parent_branch_id: str | None
    failure_stage: int | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime
    stage_results: list[StageResultResponse] = []

    model_config = {"from_attributes": True}
