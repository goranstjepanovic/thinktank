from datetime import datetime

from pydantic import BaseModel


class IdeaCreate(BaseModel):
    name: str
    description: str
    requirements: str
    constraints: str


class BranchSummary(BaseModel):
    id: str
    branch_index: int
    status: str
    current_stage: int
    approach_summary: str | None
    parent_branch_id: str | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class IdeaSummaryResponse(BaseModel):
    id: str
    name: str
    status: str
    active_branch_count: int
    viable_branch_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class IdeaDetailResponse(BaseModel):
    id: str
    name: str
    description: str
    requirements: str
    constraints: str
    status: str
    selected_branch_id: str | None = None
    selected_at: datetime | None = None
    selection_notes: str | None = None
    created_at: datetime
    updated_at: datetime
    branches: list[BranchSummary] = []

    model_config = {"from_attributes": True}
