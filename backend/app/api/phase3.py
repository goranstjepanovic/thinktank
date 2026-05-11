"""
Phase 3 API — autonomous implementation session.

Routes:
  POST /ideas/{id}/phase3          Start Phase 3 (requires SELECTED idea + READY Phase 2 session).
  GET  /ideas/{id}/phase3          Get current session status.
  POST /ideas/{id}/phase3/cancel   Cancel a running session.
"""

import asyncio
import json
import logging
import platform
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.engine import AsyncSessionLocal, get_session
from app.db.models import Idea, Phase2Session, Phase3ActivityEvent, Phase3Message, Phase3Session, SolutionBranch
from app.events import schemas as ev
from app.events.bus import event_bus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ideas", tags=["phase3"])

# Per-session queues for injecting user messages into a WAITING orchestrator
_session_user_queues: dict[str, "asyncio.Queue[str]"] = {}

# Tracked asyncio Tasks for multi-agent sessions (enables task.cancel())
_session_tasks: dict[str, "asyncio.Task"] = {}


# ---------------------------------------------------------------------------
# Pydantic output schema
# ---------------------------------------------------------------------------

class Phase3SessionOut(BaseModel):
    id: str
    idea_id: str
    phase2_session_id: str
    branch_id: str
    implementation_type: str
    status: str
    mode: str
    project_root: str | None
    output_dir: str | None
    summary: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_orm(cls, s: Phase3Session) -> "Phase3SessionOut":
        return cls(
            id=s.id,
            idea_id=s.idea_id,
            phase2_session_id=s.phase2_session_id,
            branch_id=s.branch_id,
            implementation_type=s.implementation_type,
            status=s.status,
            mode=getattr(s, "mode", "classic"),
            project_root=s.project_root,
            output_dir=s.output_dir,
            summary=s.summary,
            created_at=s.created_at.isoformat(),
            updated_at=s.updated_at.isoformat(),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_idea_or_404(idea_id: str, db: AsyncSession) -> Idea:
    r = await db.execute(select(Idea).where(Idea.id == idea_id))
    idea = r.scalar_one_or_none()
    if not idea:
        raise HTTPException(status_code=404, detail="Idea not found")
    return idea


async def _get_phase2_or_404(idea_id: str, db: AsyncSession) -> Phase2Session:
    r = await db.execute(select(Phase2Session).where(Phase2Session.idea_id == idea_id))
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="No Phase 2 session found for this idea")
    return s


async def _get_phase3_or_404(idea_id: str, db: AsyncSession) -> Phase3Session:
    r = await db.execute(select(Phase3Session).where(Phase3Session.idea_id == idea_id))
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="No Phase 3 session found for this idea")
    return s


# ---------------------------------------------------------------------------
# Background task — autonomous implementation run
# ---------------------------------------------------------------------------

async def _emit_tool_use(idea_id: str, session_id: str, tool_name: str, result: dict) -> None:
    """Emit a phase3.tool_use WebSocket event for exploration tool calls."""
    detail_map = {
        "list_files": lambda r: r.get("path", "."),
        "read_file":  lambda r: r.get("path", ""),
        "grep_files": lambda r: f'"{r.get("pattern", "")}" → {r.get("match_count", 0)} match(es)',
        "web_search": lambda r: r.get("query", ""),
    }
    detail_fn = detail_map.get(tool_name)
    if detail_fn:
        await event_bus.publish(ev.phase3_tool_use(idea_id, session_id, tool_name, detail_fn(result)))


async def _run_multi_agent_implementation(idea_id: str, session_id: str, follow_up_message: str | None = None) -> None:
    """Multi-agent mode: generate PRD first (skipped on follow-up), then run OrchestratorAgent loop."""
    from app.agents.code_generator_agent import CodeGeneratorAgent
    from app.agents.orchestrator_agent import OrchestratorAgent
    from app.main import get_inference_client
    from app.services.user_settings import get_implementations_dir
    from pathlib import Path as _Path

    async with AsyncSessionLocal() as db:
        sess_r = await db.execute(select(Phase3Session).where(Phase3Session.id == session_id))
        session = sess_r.scalar_one_or_none()
        if not session:
            return

        idea_r = await db.execute(select(Idea).where(Idea.id == idea_id))
        idea = idea_r.scalar_one_or_none()
        branch_r = await db.execute(select(SolutionBranch).where(SolutionBranch.id == session.branch_id))
        branch = branch_r.scalar_one_or_none()
        if not idea or not branch:
            return

        if session.output_dir:
            output_dir = session.output_dir
            project_root = session.project_root or ""
        else:
            project_root = idea.name.lower().replace(" ", "-").replace("_", "-")[:40] or "project"
            output_dir = str(get_implementations_dir() / idea_id / project_root)
            session.project_root = project_root
            session.output_dir = output_dir
        _Path(output_dir).mkdir(parents=True, exist_ok=True)

        session.status = "RUNNING"
        await db.commit()

        await event_bus.publish(ev.phase3_running(idea_id, session_id))
        await event_bus.publish(ev.phase3_thinking(idea_id, session_id))

        client = get_inference_client()
        gen_agent = CodeGeneratorAgent(client)

        # ── Standard on_tool_result (same as classic mode) ───────────────────
        async def on_tool_result(tool_name: str, result: dict) -> None:
            if tool_name == "plan_ready":
                payload = {"file_count": result.get("file_count", 0), "files": result.get("files", []), "commands": result.get("commands", []), "message": result.get("message", "")}
                await event_bus.publish(ev.phase3_plan_ready(idea_id, session_id, artifact_count=payload["file_count"], message=payload["message"]))
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(session_id=session_id, event_type="plan_ready", payload_json=json.dumps(payload)))
                    await adb.commit()
            elif tool_name == "pass_started":
                payload = {"file_path": result.get("file_path", ""), "file_index": result.get("file_index", 0), "total_files": result.get("total_files", 0)}
                await event_bus.publish(ev.phase3_pass_started(idea_id, session_id, **payload))
            elif tool_name == "file_edit" and result.get("success"):
                payload = {"path": result.get("path", ""), "size_bytes": result.get("size_bytes", 0)}
                await event_bus.publish(ev.phase3_file_written(idea_id, session_id, **payload))
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(session_id=session_id, event_type="file_written", payload_json=json.dumps(payload)))
                    await adb.commit()
            elif tool_name == "file_edit" and not result.get("success"):
                payload = {"path": result.get("path", ""), "detail": result.get("detail", "unknown error")}
                await event_bus.publish(ev.phase3_file_failed(idea_id, session_id, **payload))
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(session_id=session_id, event_type="file_failed", payload_json=json.dumps(payload)))
                    await adb.commit()
            elif tool_name == "run_shell":
                payload = {"command": result.get("command", ""), "exit_code": result.get("exit_code", -1), "stdout": result.get("stdout", ""), "stderr": result.get("stderr", ""), "timed_out": result.get("timed_out", False), "duration_ms": result.get("duration_ms", 0)}
                await event_bus.publish(ev.phase3_command_executed(idea_id, session_id, **payload))
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(session_id=session_id, event_type="command_executed", payload_json=json.dumps(payload)))
                    await adb.commit()
            elif tool_name == "shell_stop":
                await event_bus.publish(ev.phase3_shell_stop(idea_id, session_id, handle=result.get("handle", ""), pid=result.get("pid"), stopped=result.get("stopped", False), exit_code=result.get("exit_code"), message=result.get("message", "")))
            elif tool_name == "verify_started":
                await event_bus.publish(ev.phase3_verifying(idea_id, session_id, result.get("file_count", 0)))
            elif tool_name == "plan_warnings":
                await event_bus.publish(ev.phase3_plan_warnings(idea_id, session_id, result.get("warnings", [])))
            elif tool_name == "syntax_check":
                await event_bus.publish(ev.phase3_syntax_check(idea_id, session_id, result.get("path", ""), result.get("passed", True), result.get("error", ""), result.get("retrying", False)))
            elif tool_name in ("list_files", "read_file", "grep_files", "web_search"):
                await _emit_tool_use(idea_id, session_id, tool_name, result)

        # ── Orchestrator-level event handler ─────────────────────────────────
        async def on_orchestrator_event(event_type: str, data: dict) -> None:
            if event_type == "orchestrator_thinking":
                await event_bus.publish(ev.phase3_orchestrator_thinking(idea_id, session_id))

            elif event_type == "orchestrator_tool":
                tool = data.get("tool", "")
                result = data.get("result", {})
                if tool in ("list_files", "read_file", "grep_files", "web_search"):
                    await _emit_tool_use(idea_id, session_id, tool, result)

            elif event_type == "orchestrator_message":
                content = str(data.get("content", "")).strip()
                if content:
                    await event_bus.publish(ev.phase3_orchestrator_message(idea_id, session_id, content))
                    async with AsyncSessionLocal() as adb:
                        adb.add(Phase3Message(session_id=session_id, role="assistant", content=content))
                        await adb.commit()

            elif event_type == "waiting":
                async with AsyncSessionLocal() as adb:
                    s = await adb.get(Phase3Session, session_id)
                    if s:
                        s.status = "WAITING"
                        await adb.commit()
                await event_bus.publish(ev.phase3_waiting(idea_id, session_id))

            elif event_type == "orchestrator_running":
                async with AsyncSessionLocal() as adb:
                    s = await adb.get(Phase3Session, session_id)
                    if s:
                        s.status = "RUNNING"
                        await adb.commit()
                await event_bus.publish(ev.phase3_running(idea_id, session_id))

            elif event_type == "sub_agent_queued":
                task_id = str(data.get("task_id", ""))
                title = str(data.get("title", ""))
                agent_id = str(data.get("agent_id", ""))
                await event_bus.publish(ev.phase3_sub_agent_queued(idea_id, session_id, task_id, title, agent_id))
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(session_id=session_id, event_type="sub_agent_queued", payload_json=json.dumps({"task_id": task_id, "title": title, "agent_id": agent_id})))
                    await adb.commit()

            elif event_type == "sub_agent_started":
                task_id = str(data.get("task_id", ""))
                title = str(data.get("title", ""))
                agent_id = str(data.get("agent_id", ""))
                await event_bus.publish(ev.phase3_sub_agent_started(idea_id, session_id, task_id, title, agent_id))
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(session_id=session_id, event_type="sub_agent_started", payload_json=json.dumps({"task_id": task_id, "title": title})))
                    await adb.commit()

            elif event_type == "sub_agent_update":
                task_id = str(data.get("task_id", ""))
                update_type = str(data.get("update_type", ""))
                detail = str(data.get("detail", ""))
                await event_bus.publish(ev.phase3_sub_agent_update(idea_id, session_id, task_id, update_type, detail))

            elif event_type == "sub_agent_model_fallback":
                task_id = str(data.get("task_id", ""))
                model = str(data.get("model", ""))
                attempt = int(data.get("attempt", 0))
                await event_bus.publish(ev.phase3_sub_agent_model_fallback(idea_id, session_id, task_id, model, attempt))

            elif event_type == "sub_agent_complete":
                task_id = str(data.get("task_id", ""))
                title = str(data.get("title", ""))
                summary = str(data.get("summary", ""))
                files_written = list(data.get("files_written") or [])
                commands_run = list(data.get("commands_run") or [])
                success = bool(data.get("success", True))
                blocker = data.get("blocker")
                await event_bus.publish(ev.phase3_sub_agent_complete(idea_id, session_id, task_id, summary, files_written, success, blocker))
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(session_id=session_id, event_type="sub_agent_complete", payload_json=json.dumps({"task_id": task_id, "title": title, "summary": summary, "files_written": files_written, "commands_run": commands_run, "success": success, "blocker": blocker})))
                    await adb.commit()

        # ── Step 1: Generate PRD (skip on follow-up iterations) ──────────────
        prd_path = _Path(output_dir) / "docs" / "PRD.md"
        if not follow_up_message:
            try:
                await gen_agent.generate_prd(db, session, idea, branch, on_tool_result)
            except Exception as e:
                logger.warning("multi_agent: PRD generation failed: %s — continuing", e)

        # ── Step 2: Read PRD for orchestrator context ─────────────────────────
        try:
            prd_content = prd_path.read_text(encoding="utf-8") if prd_path.is_file() else "(PRD not available)"
        except Exception:
            prd_content = "(PRD not available)"

        # ── Step 3: Run orchestrator loop ─────────────────────────────────────
        user_queue: asyncio.Queue[str] = asyncio.Queue()
        _session_user_queues[session_id] = user_queue

        orch_agent = OrchestratorAgent(client)
        try:
            summary = await orch_agent.run(
                db, session, idea, branch,
                prd_content, on_tool_result, on_orchestrator_event, user_queue,
                follow_up_message=follow_up_message,
            )
        except asyncio.CancelledError:
            logger.info("multi_agent: session %s cancelled by user", session_id[:8])
            async with AsyncSessionLocal() as adb:
                s = await adb.get(Phase3Session, session_id)
                if s and s.status not in ("COMPLETE", "FAILED"):
                    s.status = "FAILED"
                    s.summary = "Cancelled by user"
                    await adb.commit()
            raise
        except Exception as e:
            logger.error("multi_agent: orchestrator failed for session %s: %s", session_id[:8], e)
            async with AsyncSessionLocal() as adb:
                s = await adb.get(Phase3Session, session_id)
                if s:
                    s.status = "FAILED"
                    s.summary = str(e)
                    await adb.commit()
            await event_bus.publish(ev.phase3_error(idea_id, session_id, str(e)))
            return
        finally:
            _session_user_queues.pop(session_id, None)
            _session_tasks.pop(session_id, None)

        async with AsyncSessionLocal() as adb:
            s = await adb.get(Phase3Session, session_id)
            if s:
                s.status = "COMPLETE"
                s.summary = summary
                await adb.commit()

        await event_bus.publish(ev.phase3_complete(idea_id, session_id, summary=summary, output_dir=output_dir))
        logger.info("multi_agent: complete for session %s", session_id[:8])


async def _run_implementation(idea_id: str, session_id: str) -> None:
    from app.agents.code_generator_agent import CodeGeneratorAgent
    from app.main import get_inference_client

    async with AsyncSessionLocal() as db:
        sess_r = await db.execute(select(Phase3Session).where(Phase3Session.id == session_id))
        session = sess_r.scalar_one_or_none()
        if not session:
            return

        idea_r = await db.execute(select(Idea).where(Idea.id == idea_id))
        idea = idea_r.scalar_one_or_none()
        branch_r = await db.execute(select(SolutionBranch).where(SolutionBranch.id == session.branch_id))
        branch = branch_r.scalar_one_or_none()
        if not idea or not branch:
            return

        # Determine output directory and set up session
        from app.services.user_settings import get_implementations_dir
        project_root = idea.name.lower().replace(" ", "-").replace("_", "-")[:40] or "project"
        output_dir = str(get_implementations_dir() / idea_id / project_root)
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        session.project_root = project_root
        session.output_dir = output_dir
        session.status = "RUNNING"
        await db.commit()

        await event_bus.publish(ev.phase3_running(idea_id, session_id))
        await event_bus.publish(ev.phase3_thinking(idea_id, session_id))

        # Callback: emit WebSocket events and persist activity to DB
        async def on_tool_result(tool_name: str, result: dict) -> None:
            if tool_name == "plan_ready":
                payload = {
                    "file_count": result.get("file_count", 0),
                    "files": result.get("files", []),
                    "commands": result.get("commands", []),
                    "message": result.get("message", ""),
                }
                await event_bus.publish(
                    ev.phase3_plan_ready(idea_id, session_id, artifact_count=payload["file_count"], message=payload["message"])
                )
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(
                        session_id=session_id,
                        event_type="plan_ready",
                        payload_json=json.dumps(payload),
                    ))
                    await adb.commit()
            elif tool_name == "pass_started":
                payload = {
                    "file_path": result.get("file_path", ""),
                    "file_index": result.get("file_index", 0),
                    "total_files": result.get("total_files", 0),
                }
                await event_bus.publish(
                    ev.phase3_pass_started(idea_id, session_id, **payload)
                )
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(
                        session_id=session_id,
                        event_type="pass_started",
                        payload_json=json.dumps(payload),
                    ))
                    await adb.commit()
            elif tool_name == "file_edit" and result.get("success"):
                payload = {
                    "path": result.get("path", ""),
                    "size_bytes": result.get("size_bytes", 0),
                }
                await event_bus.publish(
                    ev.phase3_file_written(idea_id, session_id, **payload)
                )
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(
                        session_id=session_id,
                        event_type="file_written",
                        payload_json=json.dumps(payload),
                    ))
                    await adb.commit()
            elif tool_name == "file_edit" and not result.get("success"):
                payload = {
                    "path": result.get("path", ""),
                    "detail": result.get("detail", "unknown error"),
                }
                await event_bus.publish(
                    ev.phase3_file_failed(idea_id, session_id, **payload)
                )
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(
                        session_id=session_id,
                        event_type="file_failed",
                        payload_json=json.dumps(payload),
                    ))
                    await adb.commit()
            elif tool_name == "run_shell":
                payload = {
                    "command": result.get("command", ""),
                    "exit_code": result.get("exit_code", -1),
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                    "timed_out": result.get("timed_out", False),
                    "duration_ms": result.get("duration_ms", 0),
                }
                await event_bus.publish(
                    ev.phase3_command_executed(idea_id, session_id, **payload)
                )
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(
                        session_id=session_id,
                        event_type="command_executed",
                        payload_json=json.dumps(payload),
                    ))
                    await adb.commit()
            elif tool_name == "shell_stop":
                await event_bus.publish(ev.phase3_shell_stop(
                    idea_id, session_id,
                    handle=result.get("handle", ""),
                    pid=result.get("pid"),
                    stopped=result.get("stopped", False),
                    exit_code=result.get("exit_code"),
                    message=result.get("message", ""),
                ))
            elif tool_name == "verify_started":
                await event_bus.publish(ev.phase3_verifying(idea_id, session_id, result.get("file_count", 0)))
            elif tool_name == "plan_warnings":
                await event_bus.publish(ev.phase3_plan_warnings(idea_id, session_id, result.get("warnings", [])))
            elif tool_name == "syntax_check":
                await event_bus.publish(ev.phase3_syntax_check(idea_id, session_id, result.get("path", ""), result.get("passed", True), result.get("error", ""), result.get("retrying", False)))
            elif tool_name in ("list_files", "read_file", "grep_files", "web_search"):
                await _emit_tool_use(idea_id, session_id, tool_name, result)

        agent = CodeGeneratorAgent(get_inference_client())
        try:
            summary = await agent.run_implementation(db, session, idea, branch, on_tool_result)
        except Exception as e:
            logger.error("Phase3 implementation failed for session %s: %s", session_id[:8], e)
            session.status = "FAILED"
            session.summary = str(e)
            db.add(Phase3ActivityEvent(
                session_id=session_id,
                event_type="error",
                payload_json=json.dumps({"message": str(e)}),
            ))
            await db.commit()
            await event_bus.publish(ev.phase3_error(idea_id, session_id, str(e)))
            return

        session.status = "COMPLETE"
        session.summary = summary
        await db.commit()

        await event_bus.publish(ev.phase3_complete(idea_id, session_id, summary=summary, output_dir=output_dir))
        logger.info("Phase3 complete for session %s — output: %s", session_id[:8], output_dir)


async def _run_prd_only(idea_id: str, session_id: str) -> None:
    """PRD-only mode: generate a single standalone PRD.md, no code."""
    from app.agents.prd_only_agent import PrdOnlyAgent
    from app.main import get_inference_client
    from app.services.user_settings import get_implementations_dir

    async with AsyncSessionLocal() as db:
        sess_r = await db.execute(select(Phase3Session).where(Phase3Session.id == session_id))
        session = sess_r.scalar_one_or_none()
        if not session:
            return

        idea_r = await db.execute(select(Idea).where(Idea.id == idea_id))
        idea = idea_r.scalar_one_or_none()
        branch_r = await db.execute(select(SolutionBranch).where(SolutionBranch.id == session.branch_id))
        branch = branch_r.scalar_one_or_none()
        if not idea or not branch:
            return

        project_root = idea.name.lower().replace(" ", "-").replace("_", "-")[:40] or "project"
        output_dir = str(get_implementations_dir() / idea_id / project_root)
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        session.project_root = project_root
        session.output_dir = output_dir
        session.status = "RUNNING"
        await db.commit()

        await event_bus.publish(ev.phase3_running(idea_id, session_id))
        await event_bus.publish(ev.phase3_thinking(idea_id, session_id))

        async def on_tool_result(tool_name: str, result: dict) -> None:
            if tool_name == "file_edit" and result.get("success"):
                payload = {"path": result.get("path", ""), "size_bytes": result.get("size_bytes", 0)}
                await event_bus.publish(ev.phase3_file_written(idea_id, session_id, **payload))
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(
                        session_id=session_id, event_type="file_written",
                        payload_json=json.dumps(payload),
                    ))
                    await adb.commit()
            elif tool_name == "web_search":
                await _emit_tool_use(idea_id, session_id, "web_search", result)

        agent = PrdOnlyAgent(get_inference_client())
        try:
            success = await agent.run(db, session, idea, branch, on_tool_result)
        except Exception as e:
            logger.error("prd_only: failed for session %s: %s", session_id[:8], e)
            session.status = "FAILED"
            session.summary = str(e)
            await db.commit()
            await event_bus.publish(ev.phase3_error(idea_id, session_id, str(e)))
            return

        summary = "PRD generated successfully." if success else "PRD generation failed."
        session.status = "COMPLETE"
        session.summary = summary
        await db.commit()

        await event_bus.publish(ev.phase3_complete(idea_id, session_id, summary=summary, output_dir=output_dir))
        logger.info("prd_only: complete for session %s", session_id[:8])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class StartPhase3Body(BaseModel):
    mode: str = "classic"


@router.post("/{idea_id}/phase3", response_model=Phase3SessionOut, status_code=201)
async def start_phase3(
    idea_id: str,
    background_tasks: BackgroundTasks,
    body: StartPhase3Body = Body(default=StartPhase3Body()),
    db: AsyncSession = Depends(get_session),
):
    """
    Start a Phase 3 autonomous implementation session.
    Requires a SELECTED idea with a READY (or later) Phase 2 session.
    Idempotent — returns the existing session if one already exists.
    """
    idea = await _get_idea_or_404(idea_id, db)
    if idea.status != "SELECTED":
        raise HTTPException(status_code=409, detail=f"Idea is {idea.status}; Phase 3 requires SELECTED")

    phase2 = await _get_phase2_or_404(idea_id, db)
    if phase2.status not in ("READY", "IMPLEMENTING", "COMPLETE"):
        raise HTTPException(
            status_code=409,
            detail=f"Phase 2 session is {phase2.status}; must be READY before starting Phase 3",
        )

    # Return existing active session; delete and recreate if previously FAILED
    existing_r = await db.execute(select(Phase3Session).where(Phase3Session.idea_id == idea_id))
    existing = existing_r.scalar_one_or_none()
    if existing:
        if existing.status not in ('FAILED',):
            return Phase3SessionOut.from_orm(existing)
        await db.delete(existing)
        await db.commit()

    if not idea.selected_branch_id:
        raise HTTPException(status_code=409, detail="No branch selected for this idea")

    mode = body.mode if body.mode in ("classic", "multi_agent", "prd_only") else "classic"
    session = Phase3Session(
        idea_id=idea_id,
        phase2_session_id=phase2.id,
        branch_id=idea.selected_branch_id,
        status="PLANNING",
        mode=mode,
    )
    db.add(session)

    phase2.status = "IMPLEMENTING"
    await db.commit()
    await db.refresh(session)

    await event_bus.publish(ev.phase3_started(idea_id, session.id))
    if mode == "multi_agent":
        task = asyncio.ensure_future(_run_multi_agent_implementation(idea_id, session.id))
        _session_tasks[session.id] = task
    elif mode == "prd_only":
        background_tasks.add_task(_run_prd_only, idea_id, session.id)
    else:
        background_tasks.add_task(_run_implementation, idea_id, session.id)

    return Phase3SessionOut.from_orm(session)


@router.get("/{idea_id}/phase3", response_model=Phase3SessionOut)
async def get_phase3(idea_id: str, db: AsyncSession = Depends(get_session)):
    await _get_idea_or_404(idea_id, db)
    return Phase3SessionOut.from_orm(await _get_phase3_or_404(idea_id, db))


@router.get("/{idea_id}/phase3/activity")
async def get_phase3_activity(idea_id: str, db: AsyncSession = Depends(get_session)):
    """Return all persisted activity events for the Phase 3 session, oldest first."""
    session = await _get_phase3_or_404(idea_id, db)
    result = await db.execute(
        select(Phase3ActivityEvent)
        .where(Phase3ActivityEvent.session_id == session.id)
        .order_by(Phase3ActivityEvent.created_at)
    )
    events = result.scalars().all()
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "payload": json.loads(e.payload_json),
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]


@router.get("/{idea_id}/phase3/files")
async def list_phase3_files(idea_id: str, dir: str = "", db: AsyncSession = Depends(get_session)):
    """Return immediate children of a directory in the Phase 3 output directory."""
    session = await _get_phase3_or_404(idea_id, db)
    if not session.output_dir or not Path(session.output_dir).is_dir():
        return {"entries": [], "output_dir": session.output_dir}

    base = Path(session.output_dir).resolve()
    target = (base / dir).resolve() if dir else base
    if not str(target).startswith(str(base)) or not target.is_dir():
        raise HTTPException(status_code=403, detail="Invalid directory")

    entries = []
    try:
        for p in sorted(target.iterdir()):
            rel = str(p.relative_to(base)).replace("\\", "/")
            if p.is_dir():
                entries.append({"path": rel, "size": 0, "type": "dir"})
            elif p.is_file():
                entries.append({"path": rel, "size": p.stat().st_size, "type": "file"})
    except Exception:
        pass
    return {"entries": entries, "output_dir": str(base)}


@router.get("/{idea_id}/phase3/file")
async def get_phase3_file(idea_id: str, path: str, db: AsyncSession = Depends(get_session)):
    """Return the content of a single file in the Phase 3 output directory."""
    session = await _get_phase3_or_404(idea_id, db)
    if not session.output_dir:
        raise HTTPException(status_code=404, detail="No output directory")

    base = Path(session.output_dir).resolve()
    target = (base / path).resolve()
    # Prevent path traversal
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=403, detail="Path outside project directory")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    size = target.stat().st_size
    # Return truncated content for very large files
    max_bytes = 256 * 1024
    try:
        content = target.read_bytes()[:max_bytes].decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"path": path, "content": content, "size": size, "truncated": size > max_bytes}


@router.get("/{idea_id}/phase3/file/raw")
async def get_phase3_file_raw(idea_id: str, path: str, db: AsyncSession = Depends(get_session)):
    """Serve a file from the Phase 3 output directory as binary (for images etc.)."""
    session = await _get_phase3_or_404(idea_id, db)
    if not session.output_dir:
        raise HTTPException(status_code=404, detail="No output directory")

    base = Path(session.output_dir).resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=403, detail="Path outside project directory")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(target)


@router.post("/{idea_id}/phase3/open-terminal")
async def open_terminal(idea_id: str, db: AsyncSession = Depends(get_session)):
    """Open a terminal window in the project output directory."""
    session = await _get_phase3_or_404(idea_id, db)
    if not session.output_dir or not Path(session.output_dir).is_dir():
        raise HTTPException(status_code=404, detail="Output directory not found")

    cwd = str(Path(session.output_dir).resolve())

    if platform.system() == "Windows":
        if shutil.which("powershell"):
            subprocess.Popen(["powershell", "-NoExit", "-Command", f"Set-Location '{cwd}'"],
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            subprocess.Popen(["cmd", "/K", f"cd /d \"{cwd}\""],
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
    elif platform.system() == "Darwin":
        script = f'tell application "Terminal" to do script "cd {shutil.quote(cwd)}"'
        subprocess.Popen(["osascript", "-e", script])
    else:
        for term in ["gnome-terminal", "xterm", "xfce4-terminal", "konsole"]:
            if shutil.which(term):
                if term == "gnome-terminal":
                    subprocess.Popen([term, "--", "bash", "--login"], cwd=cwd)
                else:
                    subprocess.Popen([term], cwd=cwd)
                break
        else:
            raise HTTPException(status_code=501, detail="No supported terminal emulator found")

    return {"opened": True}


@router.post("/{idea_id}/phase3/open-explorer")
async def open_explorer(idea_id: str, db: AsyncSession = Depends(get_session)):
    """Open the system file explorer in the project output directory."""
    session = await _get_phase3_or_404(idea_id, db)
    if not session.output_dir or not Path(session.output_dir).is_dir():
        raise HTTPException(status_code=404, detail="Output directory not found")

    cwd = str(Path(session.output_dir).resolve())

    if platform.system() == "Windows":
        subprocess.Popen(["explorer", cwd])
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", cwd])
    else:
        for fm in ["xdg-open", "nautilus", "thunar", "dolphin"]:
            if shutil.which(fm):
                subprocess.Popen([fm, cwd])
                break
        else:
            raise HTTPException(status_code=501, detail="No supported file manager found")

    return {"opened": True}


async def _run_prd_regeneration(idea_id: str, session_id: str) -> None:
    from app.agents.code_generator_agent import CodeGeneratorAgent
    from app.main import get_inference_client

    async with AsyncSessionLocal() as db:
        sess_r = await db.execute(select(Phase3Session).where(Phase3Session.id == session_id))
        session = sess_r.scalar_one_or_none()
        if not session:
            return

        idea_r = await db.execute(select(Idea).where(Idea.id == idea_id))
        idea = idea_r.scalar_one_or_none()
        branch_r = await db.execute(select(SolutionBranch).where(SolutionBranch.id == session.branch_id))
        branch = branch_r.scalar_one_or_none()
        if not idea or not branch:
            return

        session.status = "RUNNING"
        await db.commit()
        await event_bus.publish(ev.phase3_running(idea_id, session_id))
        await event_bus.publish(ev.phase3_thinking(idea_id, session_id))

        async def on_tool_result(tool_name: str, result: dict) -> None:
            if tool_name == "file_edit" and result.get("success"):
                payload = {"path": result.get("path", ""), "size_bytes": result.get("size_bytes", 0)}
                await event_bus.publish(ev.phase3_file_written(idea_id, session_id, **payload))
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(
                        session_id=session_id, event_type="file_written",
                        payload_json=json.dumps(payload),
                    ))
                    await adb.commit()
            elif tool_name == "file_edit" and not result.get("success"):
                payload = {"path": result.get("path", ""), "detail": result.get("detail", "unknown error")}
                await event_bus.publish(ev.phase3_file_failed(idea_id, session_id, **payload))
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(
                        session_id=session_id, event_type="file_failed",
                        payload_json=json.dumps(payload),
                    ))
                    await adb.commit()

        agent = CodeGeneratorAgent(get_inference_client())
        try:
            success = await agent.generate_prd(db, session, idea, branch, on_tool_result)
        except Exception as e:
            logger.error("PRD regeneration failed for session %s: %s", session_id[:8], e)
            success = False

        session.status = "COMPLETE"
        summary = "PRD regenerated successfully." if success else "PRD regeneration failed."
        session.summary = summary
        await db.commit()

        await event_bus.publish(ev.phase3_complete(idea_id, session_id, summary=summary, output_dir=session.output_dir or "", is_iteration=True))


@router.post("/{idea_id}/phase3/regenerate-prd", status_code=200)
async def regenerate_prd(
    idea_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
):
    """Re-generate docs/PRD.md from Phase 2 documents without touching other files."""
    session = await _get_phase3_or_404(idea_id, db)
    if session.status in ("PLANNING", "RUNNING"):
        raise HTTPException(status_code=409, detail="Generation already in progress")
    if not session.output_dir:
        raise HTTPException(status_code=409, detail="No output directory — run the full implementation first")

    background_tasks.add_task(_run_prd_regeneration, idea_id, session.id)
    return {"queued": True}


@router.post("/{idea_id}/phase3/cancel", status_code=200)
async def cancel_phase3(idea_id: str, db: AsyncSession = Depends(get_session)):
    """
    Cancel a RUNNING, PLANNING, or WAITING session.
    For multi-agent sessions, cancels the asyncio task so the current sub-agent
    receives CancelledError and the orchestrator stops cleanly.
    """
    session = await _get_phase3_or_404(idea_id, db)
    if session.status not in ("PLANNING", "RUNNING", "WAITING"):
        # Already done — return gracefully so stale UI stop buttons don't show errors.
        return {"cancelled": False}

    session.status = "FAILED"
    session.summary = "Cancelled by user"
    await db.commit()

    # Cancel the tracked asyncio task (multi-agent mode) so the in-flight
    # sub-agent gets CancelledError rather than running to completion.
    task = _session_tasks.get(session.id)
    if task and not task.done():
        task.cancel()

    await event_bus.publish(ev.phase3_error(idea_id, session.id, "Cancelled by user"))
    return {"cancelled": True}


# ---------------------------------------------------------------------------
# Iteration messages
# ---------------------------------------------------------------------------

class SendMessageBody(BaseModel):
    content: str


async def _run_iteration(idea_id: str, session_id: str, user_message_id: str) -> None:
    from app.agents.code_generator_agent import CodeGeneratorAgent
    from app.main import get_inference_client

    async with AsyncSessionLocal() as db:
        sess_r = await db.execute(select(Phase3Session).where(Phase3Session.id == session_id))
        session = sess_r.scalar_one_or_none()
        if not session:
            return

        idea_r = await db.execute(select(Idea).where(Idea.id == idea_id))
        idea = idea_r.scalar_one_or_none()
        branch_r = await db.execute(select(SolutionBranch).where(SolutionBranch.id == session.branch_id))
        branch = branch_r.scalar_one_or_none()
        msg_r = await db.execute(select(Phase3Message).where(Phase3Message.id == user_message_id))
        user_msg = msg_r.scalar_one_or_none()
        if not idea or not branch or not user_msg:
            return

        session.status = "RUNNING"
        await db.commit()
        await event_bus.publish(ev.phase3_running(idea_id, session_id))
        await event_bus.publish(ev.phase3_thinking(idea_id, session_id))

        async def on_tool_result(tool_name: str, result: dict) -> None:
            if tool_name == "plan_ready":
                payload = {"file_count": result.get("file_count", 0), "files": result.get("files", []), "commands": result.get("commands", []), "message": result.get("message", "")}
                await event_bus.publish(ev.phase3_plan_ready(idea_id, session_id, artifact_count=payload["file_count"], message=payload["message"]))
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(session_id=session_id, event_type="plan_ready", payload_json=json.dumps(payload)))
                    await adb.commit()
            elif tool_name == "pass_started":
                payload = {"file_path": result.get("file_path", ""), "file_index": result.get("file_index", 0), "total_files": result.get("total_files", 0)}
                await event_bus.publish(ev.phase3_pass_started(idea_id, session_id, **payload))
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(session_id=session_id, event_type="pass_started", payload_json=json.dumps(payload)))
                    await adb.commit()
            elif tool_name == "file_edit" and result.get("success"):
                payload = {"path": result.get("path", ""), "size_bytes": result.get("size_bytes", 0)}
                await event_bus.publish(ev.phase3_file_written(idea_id, session_id, **payload))
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(session_id=session_id, event_type="file_written", payload_json=json.dumps(payload)))
                    await adb.commit()
            elif tool_name == "run_shell":
                payload = {"command": result.get("command", ""), "exit_code": result.get("exit_code", -1), "stdout": result.get("stdout", ""), "stderr": result.get("stderr", ""), "timed_out": result.get("timed_out", False), "duration_ms": result.get("duration_ms", 0)}
                await event_bus.publish(ev.phase3_command_executed(idea_id, session_id, **payload))
                async with AsyncSessionLocal() as adb:
                    adb.add(Phase3ActivityEvent(session_id=session_id, event_type="command_executed", payload_json=json.dumps(payload)))
                    await adb.commit()
            elif tool_name == "shell_stop":
                await event_bus.publish(ev.phase3_shell_stop(
                    idea_id, session_id,
                    handle=result.get("handle", ""),
                    pid=result.get("pid"),
                    stopped=result.get("stopped", False),
                    exit_code=result.get("exit_code"),
                    message=result.get("message", ""),
                ))
            elif tool_name == "syntax_check":
                await event_bus.publish(ev.phase3_syntax_check(idea_id, session_id, result.get("path", ""), result.get("passed", True), result.get("error", ""), result.get("retrying", False)))
            elif tool_name in ("list_files", "read_file", "grep_files", "web_search"):
                await _emit_tool_use(idea_id, session_id, tool_name, result)

        # Load recent chat history so the agent can see prior corrections
        from app.inference.base import Message as InferenceMessage
        history_r = await db.execute(
            select(Phase3Message)
            .where(Phase3Message.session_id == session_id, Phase3Message.id != user_message_id)
            .order_by(Phase3Message.created_at.desc())
            .limit(10)
        )
        recent_msgs = list(reversed(history_r.scalars().all()))
        chat_history = [InferenceMessage(role=m.role, content=m.content) for m in recent_msgs]

        agent = CodeGeneratorAgent(get_inference_client())
        try:
            summary = await agent.run_iteration(db, session, idea, branch, user_msg.content, on_tool_result, chat_history=chat_history)
        except Exception as e:
            logger.error("Phase3 iteration failed for session %s: %s", session_id[:8], e)
            async with AsyncSessionLocal() as adb:
                assist_msg = Phase3Message(session_id=session_id, role="assistant", content=f"Iteration failed: {e}")
                adb.add(assist_msg)
                await adb.commit()
                await event_bus.publish(ev.phase3_message(idea_id, session_id, assist_msg.id, "assistant", assist_msg.content))
            async with AsyncSessionLocal() as adb:
                s = await adb.get(Phase3Session, session_id)
                if s:
                    s.status = "COMPLETE"
                    await adb.commit()
            return

        async with AsyncSessionLocal() as adb:
            assist_msg = Phase3Message(session_id=session_id, role="assistant", content=summary)
            adb.add(assist_msg)
            await adb.commit()
            await event_bus.publish(ev.phase3_message(idea_id, session_id, assist_msg.id, "assistant", summary))

        async with AsyncSessionLocal() as adb:
            s = await adb.get(Phase3Session, session_id)
            if s:
                s.status = "COMPLETE"
                s.summary = summary
                await adb.commit()
                await event_bus.publish(ev.phase3_complete(idea_id, session_id, summary=summary, output_dir=s.output_dir or "", is_iteration=True))

        logger.info("Phase3 iteration complete for session %s", session_id[:8])


@router.get("/{idea_id}/phase3/messages")
async def get_phase3_messages(idea_id: str, db: AsyncSession = Depends(get_session)):
    session = await _get_phase3_or_404(idea_id, db)
    result = await db.execute(
        select(Phase3Message)
        .where(Phase3Message.session_id == session.id)
        .order_by(Phase3Message.created_at)
    )
    msgs = result.scalars().all()
    return [{"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at.isoformat()} for m in msgs]


@router.post("/{idea_id}/phase3/messages", status_code=201)
async def send_phase3_message(
    idea_id: str,
    body: SendMessageBody,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
):
    session = await _get_phase3_or_404(idea_id, db)
    if not body.content.strip():
        raise HTTPException(status_code=422, detail="Message content cannot be empty")

    # Multi-agent active (WAITING or RUNNING): inject message into the orchestrator's queue.
    # WAITING  → transition back to RUNNING so the orchestrator resumes.
    # RUNNING  → queue the message; the orchestrator drains it at the next round via get_nowait().
    if session.status in ("WAITING", "RUNNING") and session.id in _session_user_queues:
        msg = Phase3Message(session_id=session.id, role="user", content=body.content.strip())
        db.add(msg)
        if session.status == "WAITING":
            session.status = "RUNNING"
            await event_bus.publish(ev.phase3_running(idea_id, session.id))
        await db.commit()
        await db.refresh(msg)
        await event_bus.publish(ev.phase3_message(idea_id, session.id, msg.id, "user", msg.content))
        _session_user_queues[session.id].put_nowait(msg.content)
        return {"id": msg.id, "role": msg.role, "content": msg.content, "created_at": msg.created_at.isoformat()}

    # WAITING with no live queue means the server restarted (or the background task crashed)
    # while the orchestrator was paused. Recover by treating the message as a follow-up that
    # restarts the orchestrator — same path as sending a message to a completed session.
    if session.status == "WAITING" and session.id not in _session_user_queues:
        logger.warning(
            "phase3: session %s is WAITING but has no live queue — stale state from restart/crash, recovering",
            session.id,
        )
        session.status = "DONE"
        await db.commit()
        # Fall through to the normal follow-up / iteration path below

    if session.status in ("RUNNING", "PLANNING"):
        raise HTTPException(status_code=409, detail="Generation already in progress")

    msg = Phase3Message(session_id=session.id, role="user", content=body.content.strip())
    db.add(msg)
    await db.commit()
    await db.refresh(msg)

    await event_bus.publish(ev.phase3_message(idea_id, session.id, msg.id, "user", msg.content))

    mode = getattr(session, "mode", "classic")
    if mode == "multi_agent":
        background_tasks.add_task(_run_multi_agent_implementation, idea_id, session.id, msg.content)
    else:
        background_tasks.add_task(_run_iteration, idea_id, session.id, msg.id)

    return {"id": msg.id, "role": msg.role, "content": msg.content, "created_at": msg.created_at.isoformat()}
