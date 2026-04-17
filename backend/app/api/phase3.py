"""
Phase 3 API — autonomous implementation session.

Routes:
  POST /ideas/{id}/phase3          Start Phase 3 (requires SELECTED idea + READY Phase 2 session).
  GET  /ideas/{id}/phase3          Get current session status.
  POST /ideas/{id}/phase3/cancel   Cancel a running session.
"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/{idea_id}/phase3", response_model=Phase3SessionOut, status_code=201)
async def start_phase3(
    idea_id: str,
    background_tasks: BackgroundTasks,
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

    session = Phase3Session(
        idea_id=idea_id,
        phase2_session_id=phase2.id,
        branch_id=idea.selected_branch_id,
        status="PLANNING",
    )
    db.add(session)

    phase2.status = "IMPLEMENTING"
    await db.commit()
    await db.refresh(session)

    await event_bus.publish(ev.phase3_started(idea_id, session.id))
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
async def list_phase3_files(idea_id: str, db: AsyncSession = Depends(get_session)):
    """Return a recursive list of files in the Phase 3 output directory."""
    session = await _get_phase3_or_404(idea_id, db)
    if not session.output_dir or not Path(session.output_dir).is_dir():
        return {"files": [], "output_dir": session.output_dir}

    base = Path(session.output_dir)
    files = []
    try:
        for p in sorted(base.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(base)).replace("\\", "/")
                files.append({"path": rel, "size": p.stat().st_size})
    except Exception:
        pass
    return {"files": files, "output_dir": str(base)}


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


@router.post("/{idea_id}/phase3/cancel", status_code=200)
async def cancel_phase3(idea_id: str, db: AsyncSession = Depends(get_session)):
    """
    Mark a RUNNING or PLANNING session as FAILED/cancelled.
    Note: this marks the DB status but cannot interrupt an in-flight background task.
    """
    session = await _get_phase3_or_404(idea_id, db)
    if session.status not in ("PLANNING", "RUNNING"):
        raise HTTPException(status_code=409, detail=f"Session is {session.status}; cannot cancel")

    session.status = "FAILED"
    session.summary = "Cancelled by user"
    await db.commit()

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
                await adb.commit()

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
    if session.status == "RUNNING" or session.status == "PLANNING":
        raise HTTPException(status_code=409, detail="Generation already in progress")
    if not body.content.strip():
        raise HTTPException(status_code=422, detail="Message content cannot be empty")

    msg = Phase3Message(session_id=session.id, role="user", content=body.content.strip())
    db.add(msg)
    await db.commit()
    await db.refresh(msg)

    await event_bus.publish(ev.phase3_message(idea_id, session.id, msg.id, "user", msg.content))
    background_tasks.add_task(_run_iteration, idea_id, session.id, msg.id)

    return {"id": msg.id, "role": msg.role, "content": msg.content, "created_at": msg.created_at.isoformat()}
