import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.engine import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Idea(Base):
    __tablename__ = "ideas"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    requirements: Mapped[str] = mapped_column(Text, nullable=False)
    constraints: Mapped[str] = mapped_column(Text, nullable=False)
    # QUEUED | RUNNING | PAUSED | CONVERGED | ABANDONED | SELECTED
    status: Mapped[str] = mapped_column(Text, nullable=False, default="QUEUED")
    selected_branch_id: Mapped[str | None] = mapped_column(Text, ForeignKey("solution_branches.id"), nullable=True)
    selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    selection_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    branches: Mapped[list["SolutionBranch"]] = relationship(back_populates="idea", cascade="all, delete-orphan", foreign_keys="[SolutionBranch.idea_id]")
    model_calls: Mapped[list["ModelCall"]] = relationship(back_populates="idea", cascade="all, delete-orphan")
    failure_analyses: Mapped[list["FailureAnalysis"]] = relationship(back_populates="idea", cascade="all, delete-orphan")


class SolutionBranch(Base):
    __tablename__ = "solution_branches"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    idea_id: Mapped[str] = mapped_column(Text, ForeignKey("ideas.id"), nullable=False)
    parent_branch_id: Mapped[str | None] = mapped_column(Text, ForeignKey("solution_branches.id"), nullable=True)
    branch_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # QUEUED | RUNNING | PAUSED | VIABLE | FAILED | CANCELLED
    status: Mapped[str] = mapped_column(Text, nullable=False, default="QUEUED")
    current_stage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    approach_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    inherited_context: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    failure_stage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    idea: Mapped["Idea"] = relationship(back_populates="branches", foreign_keys="[SolutionBranch.idea_id]")
    parent: Mapped["SolutionBranch | None"] = relationship(remote_side="SolutionBranch.id", foreign_keys="[SolutionBranch.parent_branch_id]")
    stage_results: Mapped[list["StageResult"]] = relationship(back_populates="branch", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="branch", cascade="all, delete-orphan")
    model_calls: Mapped[list["ModelCall"]] = relationship(back_populates="branch")


class StageResult(Base):
    __tablename__ = "stage_results"
    __table_args__ = (UniqueConstraint("branch_id", "stage_index"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    branch_id: Mapped[str] = mapped_column(Text, ForeignKey("solution_branches.id"), nullable=False)
    stage_index: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_name: Mapped[str] = mapped_column(Text, nullable=False)
    # PENDING | RUNNING | COMPLETED | FAILED | SKIPPED
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING")
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    branch: Mapped["SolutionBranch"] = relationship(back_populates="stage_results")
    model_calls: Mapped[list["ModelCall"]] = relationship(back_populates="stage_result")


class FailureAnalysis(Base):
    __tablename__ = "failure_analyses"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    idea_id: Mapped[str] = mapped_column(Text, ForeignKey("ideas.id"), nullable=False)
    failed_branch_id: Mapped[str] = mapped_column(Text, ForeignKey("solution_branches.id"), nullable=False)
    new_path_exists: Mapped[bool] = mapped_column(Boolean, nullable=False)
    suggested_direction: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    spawned_branch_id: Mapped[str | None] = mapped_column(Text, ForeignKey("solution_branches.id"), nullable=True)
    model_call_id: Mapped[str | None] = mapped_column(Text, ForeignKey("model_calls.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    idea: Mapped["Idea"] = relationship(back_populates="failure_analyses")


class ModelCall(Base):
    __tablename__ = "model_calls"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    idea_id: Mapped[str] = mapped_column(Text, ForeignKey("ideas.id"), nullable=False)
    branch_id: Mapped[str | None] = mapped_column(Text, ForeignKey("solution_branches.id"), nullable=True)
    stage_result_id: Mapped[str | None] = mapped_column(Text, ForeignKey("stage_results.id"), nullable=True)
    # STAGE | FAILURE_ANALYSIS | PHASE2 | PHASE3 | SCRIPT_EXECUTION | WEB_SEARCH | FILE_EDIT | SHELL_EXECUTION
    call_type: Mapped[str] = mapped_column(Text, nullable=False, default="STAGE")
    call_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    backend: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_json: Mapped[str] = mapped_column(Text, nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_prompt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_completion: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    idea: Mapped["Idea"] = relationship(back_populates="model_calls")
    branch: Mapped["SolutionBranch | None"] = relationship(back_populates="model_calls")
    stage_result: Mapped["StageResult | None"] = relationship(back_populates="model_calls")


class Phase2Session(Base):
    __tablename__ = "phase2_sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    idea_id: Mapped[str] = mapped_column(Text, ForeignKey("ideas.id"), nullable=False)
    branch_id: Mapped[str] = mapped_column(Text, ForeignKey("solution_branches.id"), nullable=False)
    # RESOLVING | READY | IMPLEMENTING | COMPLETE
    status: Mapped[str] = mapped_column(Text, nullable=False, default="RESOLVING")
    # Generated when status transitions to READY — structured summary of all Q&A decisions.
    # This becomes the primary context for implementation calls (not the raw conversation).
    resolution_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    messages: Mapped[list["Phase2Message"]] = relationship(back_populates="session", cascade="all, delete-orphan", order_by="Phase2Message.created_at")


class Phase2Message(Base):
    __tablename__ = "phase2_messages"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(Text, ForeignKey("phase2_sessions.id"), nullable=False)
    # user | assistant
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    session: Mapped["Phase2Session"] = relationship(back_populates="messages")


class Phase3Session(Base):
    __tablename__ = "phase3_sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    idea_id: Mapped[str] = mapped_column(Text, ForeignKey("ideas.id"), nullable=False)
    phase2_session_id: Mapped[str] = mapped_column(Text, ForeignKey("phase2_sessions.id"), nullable=False)
    branch_id: Mapped[str] = mapped_column(Text, ForeignKey("solution_branches.id"), nullable=False)
    # SOFTWARE | HARDWARE
    implementation_type: Mapped[str] = mapped_column(Text, nullable=False, default="SOFTWARE")
    # PLANNING | RUNNING | WAITING | COMPLETE | FAILED
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PLANNING")
    # classic | multi_agent
    mode: Mapped[str] = mapped_column(Text, nullable=False, default="classic")
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_root: Mapped[str | None] = mapped_column(Text, nullable=True)  # e.g. "my-project"
    output_dir: Mapped[str | None] = mapped_column(Text, nullable=True)    # absolute path on disk
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)       # completion summary
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    artifacts: Mapped[list["ImplementationArtifact"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ImplementationArtifact.order_index",
    )
    activity: Mapped[list["Phase3ActivityEvent"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Phase3ActivityEvent.created_at",
    )
    messages: Mapped[list["Phase3Message"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Phase3Message.created_at",
    )


class Phase3Message(Base):
    __tablename__ = "phase3_messages"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(Text, ForeignKey("phase3_sessions.id"), nullable=False)
    # 'user' | 'assistant'
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    session: Mapped["Phase3Session"] = relationship(back_populates="messages")


class Phase3ActivityEvent(Base):
    __tablename__ = "phase3_activity"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(Text, ForeignKey("phase3_sessions.id"), nullable=False)
    # 'file_written' | 'command_executed' | 'error'
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)  # serialised event payload
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    session: Mapped["Phase3Session"] = relationship(back_populates="activity")


class ImplementationArtifact(Base):
    __tablename__ = "implementation_artifacts"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(Text, ForeignKey("phase3_sessions.id"), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False)  # relative to project_root
    # SCAFFOLD | CODE | TEST | CONFIG | DOCS
    artifact_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)      # generated file content
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)  # agent's rationale
    # PENDING | GENERATED | ACCEPTED | REVISION_REQUESTED | SKIPPED
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING")
    user_notes: Mapped[str | None] = mapped_column(Text, nullable=True)   # feedback for revision
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    session: Mapped["Phase3Session"] = relationship(back_populates="artifacts")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    idea_id: Mapped[str] = mapped_column(Text, ForeignKey("ideas.id"), nullable=False)
    branch_id: Mapped[str] = mapped_column(Text, ForeignKey("solution_branches.id"), nullable=False)
    # EXECUTIVE_SUMMARY | ARCHITECTURE_OVERVIEW | COMPONENT_SPECS |
    # REQUIREMENTS_TRACEABILITY | RISK_REGISTER | IMPLEMENTATION_ROADMAP | OPEN_QUESTIONS
    doc_type: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    branch: Mapped["SolutionBranch"] = relationship(back_populates="documents")
