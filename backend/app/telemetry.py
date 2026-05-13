import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_project_ctx: ContextVar[dict[str, str]] = ContextVar("telemetry_project", default={})
_call_ctx: ContextVar[dict[str, Any]] = ContextVar("telemetry_call", default={})
_suppress_next: ContextVar[bool] = ContextVar("telemetry_suppress_next", default=False)
_tool_counts_ctx: ContextVar[dict[str, int]] = ContextVar("telemetry_tool_counts", default={})

_log_path: Path | None = None


def configure(path: str | Path) -> None:
    global _log_path
    _log_path = Path(path)
    _log_path.parent.mkdir(parents=True, exist_ok=True)


def set_project(project_id: str, project_name: str = "") -> None:
    """Set the current project context. Call once per orchestrator run."""
    _project_ctx.set({"id": project_id, "name": project_name})


def set_call_context(
    is_fallback: bool = False,
    fallback_from: str | None = None,
    model_type: str | None = None,
    task_id: str | None = None,
) -> None:
    """Tag the next model call with fallback/task metadata. Call before each attempt."""
    _call_ctx.set({
        "is_fallback": is_fallback,
        "fallback_from": fallback_from,
        "model_type": model_type,
        "task_id": task_id,
    })


def suppress_next_call() -> None:
    """Suppress the next log_call — caller will log the task-level outcome itself."""
    _suppress_next.set(True)


def set_tool_counts(counts: dict[str, int]) -> None:
    """Store tool call counts from call_with_tools so log_call can include them."""
    _tool_counts_ctx.set(dict(counts))


def clear_suppress() -> None:
    """Clear a pending suppress flag without consuming it via log_call.

    Call this before a manual log_call to ensure it runs even if the
    inner call_with_tools threw before reaching its own log_call.
    """
    _suppress_next.set(False)


def log_call(
    *,
    stage: str,
    model: str,
    backend: str,
    duration_ms: int | None,
    success: bool,
    error: str | None = None,
    tokens_prompt: int | None = None,
    tokens_completion: int | None = None,
    _ctx: dict | None = None,
) -> None:
    """
    Append one JSONL record to the telemetry log.

    Non-blocking: file append is synchronous but completes in <1 ms.
    All errors are swallowed so telemetry can never crash the pipeline.

    Record fields:
      ts              ISO-8601 UTC timestamp
      project_id      Idea UUID
      project_name    Idea title (human-readable grouping key)
      stage           Stage key from models.yaml (e.g. "phase3_sub_agent")
      model           Model tag actually used (may differ from stage default)
      backend         Driver used ("ollama", "openvino", "llamacpp")
      duration_ms     Inference wall time in milliseconds (null on error)
      success         False if the call raised an error
      is_fallback     True when this was a retry with a different model
      fallback_from   The model that failed on the previous attempt
      tokens_prompt   Prompt token count (null if driver doesn't report)
      tokens_completion  Completion token count (null if driver doesn't report)
      error           First 200 chars of the error message (null on success)
    """
    if _log_path is None:
        return
    if _suppress_next.get():
        _suppress_next.set(False)
        return

    proj = _project_ctx.get()
    if _ctx is not None:
        extra = _ctx  # caller supplies context explicitly; _call_ctx is left intact for the outer log_call
    else:
        extra = _call_ctx.get()
        _call_ctx.set({})  # consume — prevent bleed into subsequent calls in the same coroutine context
    tool_counts = _tool_counts_ctx.get()
    _tool_counts_ctx.set({})  # consume

    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "project_id": proj.get("id", ""),
        "project_name": proj.get("name", ""),
        "stage": stage,
        "model": model,
        "backend": backend,
        "duration_ms": duration_ms,
        "success": success,
        "is_fallback": extra.get("is_fallback", False),
        "fallback_from": extra.get("fallback_from"),
        "model_type": extra.get("model_type"),
        "task_id": extra.get("task_id"),
        "tokens_prompt": tokens_prompt,
        "tokens_completion": tokens_completion,
        "error": error[:200] if error else None,
        "tool_calls": tool_counts if tool_counts else None,
    }

    try:
        with open(_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("telemetry write error: %s", exc)
