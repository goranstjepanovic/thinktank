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
_last_call_tokens_ctx: ContextVar[tuple[int | None, int | None]] = ContextVar("telemetry_last_tokens", default=(None, None))

_log_path: Path | None = None


def configure(path: str | Path) -> None:
    global _log_path
    _log_path = Path(path)
    _log_path.parent.mkdir(parents=True, exist_ok=True)


def set_project(project_id: str, project_name: str = "", project_type: str = "") -> None:
    """Set the current project context. Call once per orchestrator run."""
    _project_ctx.set({"id": project_id, "name": project_name, "type": project_type})


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


def set_last_call_tokens(tokens_prompt: int | None, tokens_completion: int | None) -> None:
    """Capture token counts from the most recent inference call (round 0 only).

    Called by _log_call before suppress_next_call can discard the record, so
    callers that suppress automatic logging can still retrieve token counts for
    their own manual log_call via get_last_call_tokens().
    """
    _last_call_tokens_ctx.set((tokens_prompt, tokens_completion))


def get_last_call_tokens() -> tuple[int | None, int | None]:
    """Return and clear the token counts stored by the last set_last_call_tokens call."""
    result = _last_call_tokens_ctx.get()
    _last_call_tokens_ctx.set((None, None))
    return result


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
        "project_type": proj.get("type", "") or None,
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


def rank_models(
    stage: str,
    candidates: list[str],
    min_calls: int = 5,
    min_success_rate: float = 0.15,
    project_id: str | None = None,
) -> list[str]:
    """Return candidates sorted by telemetry: success_rate DESC, avg_duration_ms ASC.

    Three tiers:
      1. Ranked — >= min_calls AND >= min_success_rate: fully trusted, sorted by
         success_rate DESC then avg_duration ASC.
      2. Partial — has some calls but below either threshold: sorted by
         success_rate DESC as a secondary signal (beats keeping YAML order).
      3. Untried — zero calls: preserved in original YAML order.

    When project_id is supplied, only records for that project are counted.
    This prevents cross-project data from distorting per-run ordering.
    """
    if not candidates or _log_path is None or not _log_path.exists():
        return candidates

    yaml_order = {m: i for i, m in enumerate(candidates)}
    stats: dict[str, dict] = {m: {"total": 0, "success": 0, "dur_sum": 0, "dur_n": 0} for m in candidates}
    try:
        with open(_log_path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("stage") != stage:
                    continue
                if project_id and rec.get("project_id") != project_id:
                    continue
                model = rec.get("model")
                if model not in stats:
                    continue
                s = stats[model]
                s["total"] += 1
                if rec.get("success"):
                    s["success"] += 1
                dur = rec.get("duration_ms")
                if dur is not None:
                    s["dur_sum"] += dur
                    s["dur_n"] += 1
    except Exception as exc:
        logger.debug("telemetry rank_models error: %s", exc)
        return candidates

    ranked, partial, untried = [], [], []
    for m in candidates:
        s = stats[m]
        total = s["total"]
        success_rate = s["success"] / total if total else 0.0
        avg_dur = s["dur_sum"] / s["dur_n"] if s["dur_n"] else float("inf")
        if total >= min_calls and success_rate >= min_success_rate:
            ranked.append((m, success_rate, avg_dur))
        elif total > 0:
            partial.append((m, success_rate, avg_dur))
        else:
            untried.append(m)

    ranked.sort(key=lambda x: (-x[1], x[2]))
    partial.sort(key=lambda x: (-x[1], x[2]))
    untried.sort(key=lambda x: yaml_order[x])

    result = [m for m, _, _ in ranked] + [m for m, _, _ in partial] + untried
    logger.debug(
        "telemetry rank_models[%s] project=%s: ranked=%s partial=%s untried=%s",
        stage, project_id or "global",
        [m for m, _, _ in ranked], [m for m, _, _ in partial], untried,
    )
    return result


def delete_project_records(project_id: str) -> int:
    """Remove all telemetry records for a project. Returns the count deleted."""
    if _log_path is None or not _log_path.exists():
        return 0
    kept: list[str] = []
    deleted = 0
    try:
        with open(_log_path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("project_id") == project_id:
                        deleted += 1
                        continue
                except json.JSONDecodeError:
                    pass
                kept.append(raw if raw.endswith("\n") else raw + "\n")
        with open(_log_path, "w", encoding="utf-8") as f:
            f.writelines(kept)
    except Exception as exc:
        logger.warning("telemetry: failed to delete records for %s: %s", project_id, exc)
    return deleted
