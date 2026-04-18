from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PipelineEvent(BaseModel):
    event_type: str
    idea_id: str
    timestamp: str = Field(default_factory=_now)
    payload: dict[str, Any] = Field(default_factory=dict)


# Convenience constructors
def branch_spawned(idea_id: str, branch_id: str, branch_index: int, approach_summary: str | None, parent_branch_id: str | None) -> PipelineEvent:
    return PipelineEvent(event_type="branch.spawned", idea_id=idea_id, payload={"branch_id": branch_id, "branch_index": branch_index, "approach_summary": approach_summary, "parent_branch_id": parent_branch_id})

def branch_started(idea_id: str, branch_id: str, branch_index: int) -> PipelineEvent:
    return PipelineEvent(event_type="branch.started", idea_id=idea_id, payload={"branch_id": branch_id, "branch_index": branch_index})

def stage_started(idea_id: str, branch_id: str, stage_index: int, stage_name: str) -> PipelineEvent:
    return PipelineEvent(event_type="stage.started", idea_id=idea_id, payload={"branch_id": branch_id, "stage_index": stage_index, "stage_name": stage_name})

def stage_completed(idea_id: str, branch_id: str, stage_index: int, stage_name: str, duration_ms: int) -> PipelineEvent:
    return PipelineEvent(event_type="stage.completed", idea_id=idea_id, payload={"branch_id": branch_id, "stage_index": stage_index, "stage_name": stage_name, "duration_ms": duration_ms})

def stage_failed(idea_id: str, branch_id: str, stage_index: int, error: str) -> PipelineEvent:
    return PipelineEvent(event_type="stage.failed", idea_id=idea_id, payload={"branch_id": branch_id, "stage_index": stage_index, "error": error})

def branch_failed(idea_id: str, branch_id: str, failure_stage: int, failure_reason: str) -> PipelineEvent:
    return PipelineEvent(event_type="branch.failed", idea_id=idea_id, payload={"branch_id": branch_id, "failure_stage": failure_stage, "failure_reason": failure_reason})

def branch_viable(idea_id: str, branch_id: str) -> PipelineEvent:
    return PipelineEvent(event_type="branch.viable", idea_id=idea_id, payload={"branch_id": branch_id})

def branch_paused(idea_id: str, branch_id: str, stage_index: int) -> PipelineEvent:
    return PipelineEvent(event_type="branch.paused", idea_id=idea_id, payload={"branch_id": branch_id, "stage_index": stage_index})

def branch_resumed(idea_id: str, branch_id: str, stage_index: int) -> PipelineEvent:
    return PipelineEvent(event_type="branch.resumed", idea_id=idea_id, payload={"branch_id": branch_id, "stage_index": stage_index})

def failure_analysis_completed(idea_id: str, failed_branch_id: str, new_path_exists: bool, spawned_branch_id: str | None) -> PipelineEvent:
    return PipelineEvent(event_type="failure_analysis.completed", idea_id=idea_id, payload={"failed_branch_id": failed_branch_id, "new_path_exists": new_path_exists, "spawned_branch_id": spawned_branch_id})

def idea_converged(idea_id: str, viable_branch_ids: list[str]) -> PipelineEvent:
    return PipelineEvent(event_type="idea.converged", idea_id=idea_id, payload={"viable_branch_ids": viable_branch_ids})

def idea_abandoned(idea_id: str) -> PipelineEvent:
    return PipelineEvent(event_type="idea.abandoned", idea_id=idea_id)

def idea_selected(idea_id: str, branch_id: str) -> PipelineEvent:
    return PipelineEvent(event_type="idea.selected", idea_id=idea_id, payload={"branch_id": branch_id})

def phase2_started(idea_id: str, session_id: str) -> PipelineEvent:
    return PipelineEvent(event_type="phase2.started", idea_id=idea_id, payload={"session_id": session_id})

def phase2_thinking(idea_id: str, session_id: str) -> PipelineEvent:
    return PipelineEvent(event_type="phase2.thinking", idea_id=idea_id, payload={"session_id": session_id})

def phase2_message(idea_id: str, session_id: str, message_id: str, role: str, content: str) -> PipelineEvent:
    return PipelineEvent(event_type="phase2.message", idea_id=idea_id,
                         payload={"session_id": session_id, "message_id": message_id, "role": role, "content": content})

def phase2_error(idea_id: str, session_id: str, error: str) -> PipelineEvent:
    return PipelineEvent(event_type="phase2.error", idea_id=idea_id, payload={"session_id": session_id, "error": error})

def phase2_status_changed(idea_id: str, session_id: str, status: str) -> PipelineEvent:
    return PipelineEvent(event_type="phase2.status_changed", idea_id=idea_id, payload={"session_id": session_id, "status": status})

def document_created(idea_id: str, branch_id: str, doc_type: str, file_path: str) -> PipelineEvent:
    return PipelineEvent(event_type="document.created", idea_id=idea_id, payload={"branch_id": branch_id, "doc_type": doc_type, "file_path": file_path})

def phase3_started(idea_id: str, session_id: str) -> PipelineEvent:
    return PipelineEvent(event_type="phase3.started", idea_id=idea_id, payload={"session_id": session_id})

def phase3_thinking(idea_id: str, session_id: str) -> PipelineEvent:
    return PipelineEvent(event_type="phase3.thinking", idea_id=idea_id, payload={"session_id": session_id})

def phase3_plan_ready(idea_id: str, session_id: str, artifact_count: int, message: str = "") -> PipelineEvent:
    return PipelineEvent(event_type="phase3.plan_ready", idea_id=idea_id, payload={"session_id": session_id, "artifact_count": artifact_count, "message": message})

def phase3_pass_started(idea_id: str, session_id: str, file_path: str, file_index: int, total_files: int) -> PipelineEvent:
    return PipelineEvent(event_type="phase3.pass_started", idea_id=idea_id, payload={"session_id": session_id, "file_path": file_path, "file_index": file_index, "total_files": total_files})

def phase3_running(idea_id: str, session_id: str) -> PipelineEvent:
    return PipelineEvent(event_type="phase3.running", idea_id=idea_id, payload={"session_id": session_id})

def phase3_file_written(idea_id: str, session_id: str, path: str, size_bytes: int) -> PipelineEvent:
    return PipelineEvent(event_type="phase3.file_written", idea_id=idea_id, payload={"session_id": session_id, "path": path, "size_bytes": size_bytes})

def phase3_file_failed(idea_id: str, session_id: str, path: str, detail: str) -> PipelineEvent:
    return PipelineEvent(event_type="phase3.file_failed", idea_id=idea_id, payload={"session_id": session_id, "path": path, "detail": detail})

def phase3_command_executed(idea_id: str, session_id: str, command: str, exit_code: int, stdout: str, stderr: str, timed_out: bool, duration_ms: int) -> PipelineEvent:
    return PipelineEvent(event_type="phase3.command_executed", idea_id=idea_id, payload={
        "session_id": session_id, "command": command, "exit_code": exit_code,
        "stdout": stdout[:2000], "stderr": stderr[:2000],
        "timed_out": timed_out, "duration_ms": duration_ms,
    })

def phase3_error(idea_id: str, session_id: str, error: str) -> PipelineEvent:
    return PipelineEvent(event_type="phase3.error", idea_id=idea_id, payload={"session_id": session_id, "error": error})

def phase3_complete(idea_id: str, session_id: str, summary: str, output_dir: str, is_iteration: bool = False) -> PipelineEvent:
    return PipelineEvent(event_type="phase3.complete", idea_id=idea_id, payload={"session_id": session_id, "summary": summary, "output_dir": output_dir, "is_iteration": is_iteration})

def phase3_shell_stop(idea_id: str, session_id: str, handle: str, pid: int | None, stopped: bool, exit_code: int | None, message: str) -> PipelineEvent:
    return PipelineEvent(event_type="phase3.shell_stop", idea_id=idea_id, payload={"session_id": session_id, "handle": handle, "pid": pid, "stopped": stopped, "exit_code": exit_code, "message": message})

def phase3_message(idea_id: str, session_id: str, message_id: str, role: str, content: str) -> PipelineEvent:
    return PipelineEvent(event_type="phase3.message", idea_id=idea_id, payload={"session_id": session_id, "message_id": message_id, "role": role, "content": content})

def phase3_tool_use(idea_id: str, session_id: str, tool: str, detail: str) -> PipelineEvent:
    return PipelineEvent(event_type="phase3.tool_use", idea_id=idea_id, payload={"session_id": session_id, "tool": tool, "detail": detail})
