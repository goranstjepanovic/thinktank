"""
Phase 2 API — interactive implementation session.

Routes:
  POST /ideas/{id}/phase2            Start (or return existing) Phase 2 session.
                                     Async: opening message arrives via WebSocket.
  GET  /ideas/{id}/phase2            Get session + full message history.
  POST /ideas/{id}/phase2/messages   Post a user message.
                                     Returns an SSE stream; chunks arrive immediately.
  POST /ideas/{id}/phase2/ready      Mark session as READY (transition from RESOLVING).
"""

import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.engine import AsyncSessionLocal, get_session
from app.db.models import Idea, Phase2Message, Phase2Session, SolutionBranch
from app.events import schemas as ev
from app.events.bus import event_bus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ideas", tags=["phase2"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class Phase2MessageOut(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    created_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm(cls, m: Phase2Message) -> "Phase2MessageOut":
        return cls(
            id=m.id,
            session_id=m.session_id,
            role=m.role,
            content=m.content,
            created_at=m.created_at.isoformat(),
        )


class Phase2SessionOut(BaseModel):
    id: str
    idea_id: str
    branch_id: str
    status: str
    resolution_summary: str | None
    created_at: str
    updated_at: str
    messages: list[Phase2MessageOut]

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm(cls, s: Phase2Session) -> "Phase2SessionOut":
        return cls(
            id=s.id,
            idea_id=s.idea_id,
            branch_id=s.branch_id,
            status=s.status,
            resolution_summary=s.resolution_summary,
            created_at=s.created_at.isoformat(),
            updated_at=s.updated_at.isoformat(),
            messages=[Phase2MessageOut.from_orm(m) for m in (s.messages or [])],
        )


class UserMessageBody(BaseModel):
    content: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_idea_or_404(idea_id: str, db: AsyncSession) -> Idea:
    result = await db.execute(select(Idea).where(Idea.id == idea_id))
    idea = result.scalar_one_or_none()
    if not idea:
        raise HTTPException(status_code=404, detail="Idea not found")
    return idea


async def _get_session_or_404(idea_id: str, db: AsyncSession) -> Phase2Session:
    result = await db.execute(
        select(Phase2Session)
        .where(Phase2Session.idea_id == idea_id)
        .options(selectinload(Phase2Session.messages))
    )
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="No Phase 2 session found for this idea")
    return s


async def _get_branch(branch_id: str, db: AsyncSession) -> SolutionBranch | None:
    result = await db.execute(select(SolutionBranch).where(SolutionBranch.id == branch_id))
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Background task: generate the opening message
# ---------------------------------------------------------------------------

async def _generate_opening(idea_id: str, session_id: str) -> None:
    from app.pipeline.phase2_agent import Phase2Agent
    from app.main import get_inference_client

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Phase2Session)
            .where(Phase2Session.id == session_id)
            .options(selectinload(Phase2Session.messages))
        )
        session = result.scalar_one_or_none()
        if not session:
            return

        idea_result = await db.execute(select(Idea).where(Idea.id == idea_id))
        idea = idea_result.scalar_one_or_none()
        branch = await _get_branch(session.branch_id, db)
        if not idea or not branch:
            return

        agent = Phase2Agent(get_inference_client())
        await event_bus.publish(ev.phase2_thinking(idea_id, session_id))

        try:
            content = await agent.generate_opening(db, session, idea, branch)
        except Exception as e:
            logger.error("Phase 2 opening generation failed: %s", e)
            await event_bus.publish(ev.phase2_error(idea_id, session_id, str(e)))
            return

        msg = Phase2Message(session_id=session_id, role="assistant", content=content)
        db.add(msg)
        await db.commit()
        await db.refresh(msg)

        await event_bus.publish(ev.phase2_message(idea_id, session_id, msg.id, "assistant", content))


# ---------------------------------------------------------------------------
# Background task: generate a response to a user message
# ---------------------------------------------------------------------------

async def _generate_response(idea_id: str, session_id: str, user_message_id: str) -> None:
    from app.pipeline.phase2_agent import Phase2Agent
    from app.main import get_inference_client

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Phase2Session)
            .where(Phase2Session.id == session_id)
            .options(selectinload(Phase2Session.messages))
        )
        session = result.scalar_one_or_none()
        if not session:
            return

        idea_result = await db.execute(select(Idea).where(Idea.id == idea_id))
        idea = idea_result.scalar_one_or_none()
        branch = await _get_branch(session.branch_id, db)
        if not idea or not branch:
            return

        # History = all messages except the last user message (which we pass separately)
        all_msgs = sorted(session.messages, key=lambda m: m.created_at)
        user_msg = next((m for m in all_msgs if m.id == user_message_id), None)
        if not user_msg:
            return
        history = [m for m in all_msgs if m.id != user_message_id]

        agent = Phase2Agent(get_inference_client())
        await event_bus.publish(ev.phase2_thinking(idea_id, session_id))

        try:
            content = await agent.respond(db, session, idea, branch, history, user_msg.content)
        except Exception as e:
            logger.error("Phase 2 response generation failed: %s", e)
            await event_bus.publish(ev.phase2_error(idea_id, session_id, str(e)))
            return

        msg = Phase2Message(session_id=session_id, role="assistant", content=content)
        db.add(msg)
        await db.commit()
        await db.refresh(msg)

        await event_bus.publish(ev.phase2_message(idea_id, session_id, msg.id, "assistant", content))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/{idea_id}/phase2", response_model=Phase2SessionOut, status_code=201)
async def start_phase2(
    idea_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
):
    """
    Start a Phase 2 session for the selected idea.
    Idempotent — returns the existing session if one already exists.
    Triggers async generation of the opening message.
    """
    idea = await _get_idea_or_404(idea_id, db)
    if idea.status != "SELECTED":
        raise HTTPException(status_code=409, detail=f"Idea is {idea.status}; Phase 2 requires a SELECTED idea")

    # Idempotent — return existing session if it matches the currently selected branch.
    # If a stale session exists for a different branch (selection was changed), delete it.
    existing = await db.execute(
        select(Phase2Session)
        .where(Phase2Session.idea_id == idea_id)
        .options(selectinload(Phase2Session.messages))
    )
    session = existing.scalar_one_or_none()
    if session:
        if session.branch_id == idea.selected_branch_id:
            return Phase2SessionOut.from_orm(session)
        # Stale session — selection changed since this session was created
        await db.delete(session)
        await db.commit()

    branch_id = idea.selected_branch_id
    if not branch_id:
        raise HTTPException(status_code=409, detail="No branch selected for this idea")

    session = Phase2Session(idea_id=idea_id, branch_id=branch_id, status="RESOLVING")
    db.add(session)
    await db.commit()
    await db.refresh(session)

    await event_bus.publish(ev.phase2_started(idea_id, session.id))
    background_tasks.add_task(_generate_opening, idea_id, session.id)

    result = await db.execute(
        select(Phase2Session)
        .where(Phase2Session.id == session.id)
        .options(selectinload(Phase2Session.messages))
    )
    session = result.scalar_one()
    return Phase2SessionOut.from_orm(session)


@router.get("/{idea_id}/phase2", response_model=Phase2SessionOut)
async def get_phase2(idea_id: str, db: AsyncSession = Depends(get_session)):
    await _get_idea_or_404(idea_id, db)
    return Phase2SessionOut.from_orm(await _get_session_or_404(idea_id, db))


@router.post("/{idea_id}/phase2/messages")
async def post_message(
    idea_id: str,
    body: UserMessageBody,
    db: AsyncSession = Depends(get_session),
):
    """
    Post a user message. Returns an SSE stream.
    Events: {"type":"chunk","content":"..."} per token, then {"type":"done","message_id":"..."}.
    """
    if not body.content.strip():
        raise HTTPException(status_code=422, detail="Message content cannot be empty")

    session_obj = await _get_session_or_404(idea_id, db)
    if session_obj.status == "COMPLETE":
        raise HTTPException(status_code=409, detail="Phase 2 session is complete")

    # Save the user message before streaming starts
    user_msg = Phase2Message(session_id=session_obj.id, role="user", content=body.content.strip())
    db.add(user_msg)
    await db.commit()
    await db.refresh(user_msg)

    # Capture values needed inside the generator (db session will be closed after return)
    session_id = session_obj.id
    branch_id = session_obj.branch_id
    user_msg_id = user_msg.id

    async def generate():
        from app.main import get_inference_client
        from app.pipeline.phase2_agent import Phase2Agent

        async with AsyncSessionLocal() as gen_db:
            idea_r = await gen_db.execute(select(Idea).where(Idea.id == idea_id))
            idea = idea_r.scalar_one_or_none()
            branch = await _get_branch(branch_id, gen_db)
            if not idea or not branch:
                yield f"data: {json.dumps({'type': 'error', 'error': 'Idea or branch not found'})}\n\n"
                return

            sess_r = await gen_db.execute(
                select(Phase2Session)
                .where(Phase2Session.id == session_id)
                .options(selectinload(Phase2Session.messages))
            )
            sess = sess_r.scalar_one_or_none()
            if not sess:
                yield f"data: {json.dumps({'type': 'error', 'error': 'Session not found'})}\n\n"
                return

            all_msgs = sorted(sess.messages, key=lambda m: m.created_at)
            history = [m for m in all_msgs if m.id != user_msg_id]
            user_content = next((m.content for m in all_msgs if m.id == user_msg_id), body.content)

            agent = Phase2Agent(get_inference_client())
            messages = await agent.build_conversation_messages(
                gen_db, idea, branch, history, user_content,
                resolution_summary=sess.resolution_summary,
            )

            full_content = ""
            try:
                async for chunk in get_inference_client().stream_text("phase2_conversation", messages):
                    full_content += chunk
                    yield f"data: {json.dumps({'type': 'chunk', 'content': chunk})}\n\n"
            except Exception as e:
                logger.error("Phase2 streaming error: %s", e)
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
                await event_bus.publish(ev.phase2_error(idea_id, session_id, str(e)))
                return

            # Persist the complete assistant message
            assist_msg = Phase2Message(session_id=session_id, role="assistant", content=full_content)
            gen_db.add(assist_msg)
            await gen_db.commit()
            await gen_db.refresh(assist_msg)

            # Notify other connected clients via WS
            await event_bus.publish(
                ev.phase2_message(idea_id, session_id, assist_msg.id, "assistant", full_content)
            )

            yield f"data: {json.dumps({'type': 'done', 'message_id': assist_msg.id})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _build_resolution_summary(idea_id: str, session_id: str) -> None:
    """Background task: generate and persist the resolution summary."""
    from app.main import get_inference_client
    from app.pipeline.phase2_agent import Phase2Agent

    # Signal the UI that generation is in progress
    await event_bus.publish(ev.phase2_thinking(idea_id, session_id))

    async with AsyncSessionLocal() as db:
        sess_r = await db.execute(
            select(Phase2Session)
            .where(Phase2Session.id == session_id)
            .options(selectinload(Phase2Session.messages))
        )
        session = sess_r.scalar_one_or_none()
        if not session:
            return

        idea_r = await db.execute(select(Idea).where(Idea.id == idea_id))
        idea = idea_r.scalar_one_or_none()
        branch = await _get_branch(session.branch_id, db)
        if not idea or not branch:
            return

        agent = Phase2Agent(get_inference_client())
        try:
            summary = await agent.generate_resolution_summary(db, session, idea, branch)
        except Exception as e:
            logger.error("Resolution summary generation failed: %s", e)
            await event_bus.publish(ev.phase2_error(idea_id, session_id, f"Summary generation failed: {e}"))
            return

        session.resolution_summary = summary
        await db.commit()

        await event_bus.publish(ev.phase2_status_changed(idea_id, session_id, "READY"))
        logger.info("Phase2 resolution summary generated for session %s", session_id[:8])


@router.post("/{idea_id}/phase2/ready", response_model=Phase2SessionOut)
async def mark_ready(
    idea_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
):
    """
    Mark the session as READY — open questions resolved, implementation may begin.
    Triggers async generation of the Resolution Summary.
    """
    session = await _get_session_or_404(idea_id, db)
    if session.status not in ("RESOLVING",):
        raise HTTPException(status_code=409, detail=f"Session is already {session.status}")

    session.status = "READY"
    await db.commit()

    # Fire-and-forget: generate the resolution summary in the background
    background_tasks.add_task(_build_resolution_summary, idea_id, session.id)

    result = await db.execute(
        select(Phase2Session)
        .where(Phase2Session.id == session.id)
        .options(selectinload(Phase2Session.messages))
    )
    return Phase2SessionOut.from_orm(result.scalar_one())
