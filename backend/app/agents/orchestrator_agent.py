"""
Phase 3 Orchestrator Agent — multi-agent mode.

The orchestrator reads the PRD, plans tasks autonomously, and delegates each task
to a SubAgent that does the actual file writing and command execution.

Flow:
  1. (Caller generates PRD first, passes content in)
  2. Orchestrator loop: inspect project state → decide next task → delegate to sub-agent
  3. Sub-agent writes files and runs commands, reporting back via callbacks
  4. Orchestrator incorporates result, decides next task or finishes
  5. If orchestrator needs user input, it sets user_message and yields to the caller;
     the caller suspends the session (WAITING) and resumes when a message arrives.
"""

import asyncio
import logging
from pathlib import Path
from typing import Callable, Awaitable

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Idea, Phase3Session, SolutionBranch
from app.inference.base import Message
from app.inference.client import InferenceClient
from app.tools.shell_runner import shell_environment_context

logger = logging.getLogger(__name__)

_MAX_ORCHESTRATOR_ROUNDS = 20
_MAX_PRD_CHARS = 24_000


def _orchestrator_system_prompt(prd_content: str) -> str:
    prd_excerpt = prd_content[:_MAX_PRD_CHARS]
    if len(prd_content) > _MAX_PRD_CHARS:
        prd_excerpt += "\n... (PRD truncated for context)"
    return (
        "You are an orchestrator agent for a software implementation pipeline.\n\n"
        "## Your role\n\n"
        "You read the PRD, check what has already been built, and delegate one concrete task at a time "
        "to a sub-agent. YOU do NOT write files or run commands — you only read the project state to "
        "understand what is done and what remains.\n\n"
        "## Available tools\n\n"
        "You may call `list_files`, `read_file`, and `grep_files` to inspect the project. "
        "Do NOT call `file_edit` or `run_shell` — those are reserved for the sub-agent.\n\n"
        "## Product Requirements Document\n\n"
        f"{prd_excerpt}\n\n"
        "## Output format\n\n"
        "After any tool use (or immediately), output a JSON object only — no prose:\n"
        '{"analysis": "what has been done and what remains", '
        '"next_task": {"id": "snake_case_id", "title": "short title ≤ 60 chars", '
        '"instruction": "complete self-contained instructions for the sub-agent — list all files to create, '
        'their purpose, any cross-file dependencies, and any commands to run"}, '
        '"done": false, "user_message": null}\n\n'
        "When all PRD sections are implemented:\n"
        '{"analysis": "all tasks complete", "next_task": null, "done": true, "user_message": null}\n\n'
        "When you need the user to make a decision before you can proceed:\n"
        '{"analysis": "...", "next_task": null, "done": false, '
        '"user_message": "the specific question or decision you need from the user"}\n\n'
        "## Task instruction guidelines\n\n"
        "The sub-agent receives only the `instruction` field and the output directory path. Make it:\n"
        "- Fully self-contained: list every file by path with its purpose\n"
        "- Tech-stack specific: name libraries, versions, and patterns to use\n"
        "- Context-aware: tell the sub-agent which existing files to read for context first\n"
        "- Command-aware: list any post-write commands (install, build, test) to run\n"
        f"- Shell environment: {shell_environment_context()}\n\n"
        "## Rules\n\n"
        "- Delegate ONE cohesive task per response (e.g. 'backend API layer', 'frontend components')\n"
        "- Before choosing the next task, call `list_files` to verify what is already on disk\n"
        "- Track which PRD sections have been implemented and ensure full coverage\n"
        "- Set `done=true` only when all PRD sections are covered by completed tasks\n"
        "- Output ONLY the JSON object — no markdown fences, no extra text\n"
    )


def _orchestrator_user_prompt(
    idea: Idea,
    branch: SolutionBranch,
    completed_tasks: list[dict],
) -> str:
    history_block = ""
    if completed_tasks:
        lines = []
        for t in completed_tasks:
            icon = "✓" if t.get("success") else "⚠"
            lines.append(f"{icon} **{t['title']}**: {t.get('summary', '')}")
            fw = t.get("files_written", [])
            if fw:
                sample = ", ".join(fw[:5])
                extra = f" (+{len(fw) - 5} more)" if len(fw) > 5 else ""
                lines.append(f"   Files: {sample}{extra}")
            if t.get("blocker"):
                lines.append(f"   ⚠ Blocker: {t['blocker']}")
        history_block = "\n\n## Completed Tasks\n\n" + "\n".join(lines)

    start_hint = (
        "No tasks have been started yet. "
        "Begin with the project scaffold: root files (README.md, .gitignore, .env.example, pyproject.toml / package.json), "
        "then proceed layer by layer."
        if not completed_tasks
        else "Call `list_files` to check what is on disk, then decide the next task."
    )

    return (
        f"PROJECT: {idea.name}\n"
        f"DESCRIPTION: {idea.description}\n\n"
        f"SELECTED APPROACH: {branch.approach_summary or 'N/A'}\n"
        f"{history_block}\n\n"
        f"{start_hint}"
    )


def _sub_agent_system_prompt() -> str:
    return (
        "You are a sub-agent implementing a specific task in a software project.\n\n"
        "## Workflow\n\n"
        "1. Read any existing files you need for context (`list_files`, `read_file`, `grep_files`)\n"
        "2. Write all files required for your task using `file_edit`\n"
        "3. Run any required commands (install dependencies, build, test) using `run_shell`\n"
        "4. Return a JSON summary when done\n\n"
        "## File writing rules\n\n"
        "- Write complete file content — never truncate or use ellipsis placeholders\n"
        "- Parent directories are created automatically; never use mkdir\n"
        "- For binary files, skip them and note it in your summary\n\n"
        "## Command rules\n\n"
        f"- Shell environment: {shell_environment_context()}\n"
        "- One command per `run_shell` call — never chain with && or ;\n"
        "- Never run servers or long-running processes\n"
        "- If a command fails, read the error and fix the root cause before retrying\n"
        "- If the same command fails twice with the same error, stop and report as a blocker\n\n"
        "## Output format\n\n"
        "When finished, output JSON only — no prose, no fences:\n"
        '{"summary": "what you built", "files_written": ["path1", "path2"], '
        '"commands_run": ["cmd1"], "success": true, "blocker": null}\n\n'
        "If blocked by a missing external dependency, service, or user decision:\n"
        '{"summary": "what you attempted", "files_written": [], "commands_run": [], '
        '"success": false, "blocker": "specific description of what is blocking"}'
    )


def _sub_agent_user_prompt(task_instruction: str, output_dir: str, idea_name: str) -> str:
    return (
        f"PROJECT: {idea_name}\n"
        f"OUTPUT DIRECTORY: {output_dir}\n\n"
        f"TASK:\n{task_instruction}\n\n"
        "Start by reading any files needed for context, then write all required files, "
        "run any specified commands, and return the JSON summary."
    )


OnToolResult = Callable[[str, dict], Awaitable[None]]
OnOrchestratorEvent = Callable[[str, dict], Awaitable[None]]


class OrchestratorAgent:
    """
    Reads the PRD, iteratively delegates tasks to SubAgents, and tracks completion.

    Callbacks:
      on_tool_result — the standard Phase 3 tool-result handler (emits file_written, etc.)
      on_orchestrator_event — handler for orchestrator-level events (thinking, sub_agent_started, etc.)
      user_message_queue — asyncio.Queue[str]; put() a message to unblock a WAITING orchestrator
    """

    def __init__(self, inference_client: InferenceClient) -> None:
        self._client = inference_client

    async def run(
        self,
        db: AsyncSession,
        session: Phase3Session,
        idea: Idea,
        branch: SolutionBranch,
        prd_content: str,
        on_tool_result: OnToolResult,
        on_orchestrator_event: OnOrchestratorEvent,
        user_message_queue: "asyncio.Queue[str]",
    ) -> str:
        output_dir = session.output_dir or ""
        completed_tasks: list[dict] = []

        for round_idx in range(_MAX_ORCHESTRATOR_ROUNDS):
            logger.info("orchestrator: round %d/%d", round_idx + 1, _MAX_ORCHESTRATOR_ROUNDS)
            await on_orchestrator_event("orchestrator_thinking", {})

            # Build orchestrator messages with accumulated context
            orch_messages = [
                Message(role="system", content=_orchestrator_system_prompt(prd_content)),
                Message(role="user", content=_orchestrator_user_prompt(idea, branch, completed_tasks)),
            ]

            async def _orch_tool_cb(tool: str, result: dict) -> None:
                await on_orchestrator_event("orchestrator_tool", {"tool": tool, "result": result})

            try:
                orch_result = await self._client.call_with_tools(
                    stage_key="phase3_orchestrator",
                    messages=orch_messages,
                    session=db,
                    idea_id=idea.id,
                    branch_id=branch.id,
                    allowed_file_dir=output_dir,
                    explore_only=True,
                    max_tool_rounds=10,
                    return_json=True,
                    call_index=round_idx * 100,
                    on_tool_result=_orch_tool_cb,
                )
            except Exception as e:
                logger.error("orchestrator: round %d failed: %s", round_idx + 1, e)
                break

            if not isinstance(orch_result, dict):
                logger.warning("orchestrator: non-dict result in round %d", round_idx + 1)
                break

            analysis = str(orch_result.get("analysis", "")).strip()
            user_message = orch_result.get("user_message")
            done = bool(orch_result.get("done", False))
            next_task = orch_result.get("next_task")

            logger.info("orchestrator: done=%s task=%s question=%s", done, next_task and next_task.get("id"), bool(user_message))

            # Orchestrator needs user input
            if user_message:
                content = str(user_message).strip()
                await on_orchestrator_event("orchestrator_message", {"content": content})
                await on_orchestrator_event("waiting", {})
                try:
                    user_reply = await asyncio.wait_for(user_message_queue.get(), timeout=3600)
                except asyncio.TimeoutError:
                    logger.warning("orchestrator: timed out waiting for user reply")
                    break
                completed_tasks.append({
                    "id": f"user_input_{round_idx}",
                    "title": "User input",
                    "summary": f"Asked: {content[:80]}… | User replied: {user_reply[:80]}",
                    "success": True,
                    "files_written": [],
                    "commands_run": [],
                })
                await on_orchestrator_event("orchestrator_running", {})
                continue

            if done or not next_task or not isinstance(next_task, dict):
                logger.info("orchestrator: signalled done after %d round(s)", round_idx + 1)
                break

            task_id = str(next_task.get("id") or f"task_{round_idx}")
            task_title = str(next_task.get("title") or f"Task {round_idx + 1}")[:80]
            task_instruction = str(next_task.get("instruction") or "")

            if not task_instruction.strip():
                logger.warning("orchestrator: empty instruction in round %d — stopping", round_idx + 1)
                break

            await on_orchestrator_event("sub_agent_started", {"task_id": task_id, "title": task_title})

            sub_result = await self._run_sub_agent(
                db, idea, branch, output_dir,
                task_id, task_title, task_instruction,
                on_tool_result, on_orchestrator_event,
            )

            await on_orchestrator_event("sub_agent_complete", {
                "task_id": task_id,
                "summary": sub_result.get("summary", ""),
                "files_written": sub_result.get("files_written", []),
                "commands_run": sub_result.get("commands_run", []),
                "success": sub_result.get("success", True),
                "blocker": sub_result.get("blocker"),
            })

            completed_tasks.append({"id": task_id, "title": task_title, **sub_result})

        # Build completion summary
        task_summaries = [t for t in completed_tasks if not t["id"].startswith("user_input_")]
        total_files = sum(len(t.get("files_written", [])) for t in task_summaries)
        n_tasks = len(task_summaries)
        summary = f"Completed {n_tasks} task(s), wrote {total_files} file(s). "
        summary += " | ".join(t["title"] for t in task_summaries[:4])
        if n_tasks > 4:
            summary += f" … (+{n_tasks - 4} more)"
        return summary.strip()

    async def _run_sub_agent(
        self,
        db: AsyncSession,
        idea: Idea,
        branch: SolutionBranch,
        output_dir: str,
        task_id: str,
        task_title: str,
        task_instruction: str,
        on_tool_result: OnToolResult,
        on_orchestrator_event: OnOrchestratorEvent,
    ) -> dict:
        logger.info("sub_agent: starting task '%s' (%s)", task_title, task_id)

        async def _wrapped_on_tool(tool_name: str, result: dict) -> None:
            # Forward to standard handler (emits file_written, command_executed events)
            await on_tool_result(tool_name, result)
            # Also emit sub_agent_update for UI
            if tool_name == "file_edit":
                detail = result.get("path", "")
            elif tool_name == "run_shell":
                detail = result.get("command", "")
            elif tool_name in ("list_files", "read_file", "grep_files", "web_search"):
                detail = result.get("path") or result.get("pattern") or result.get("query") or ""
            else:
                detail = ""
            await on_orchestrator_event("sub_agent_update", {
                "task_id": task_id,
                "update_type": tool_name,
                "detail": str(detail),
                "result": result,
            })

        try:
            result = await self._client.call_with_tools(
                stage_key="phase3_sub_agent",
                messages=[
                    Message(role="system", content=_sub_agent_system_prompt()),
                    Message(role="user", content=_sub_agent_user_prompt(task_instruction, output_dir, idea.name)),
                ],
                session=db,
                idea_id=idea.id,
                branch_id=branch.id,
                allowed_file_dir=output_dir,
                explore_only=False,
                max_tool_rounds=60,
                return_json=True,
                call_index=0,
                on_tool_result=_wrapped_on_tool,
            )
        except Exception as e:
            logger.error("sub_agent: task '%s' failed: %s", task_id, e)
            return {
                "summary": f"Task failed with error: {e}",
                "files_written": [],
                "commands_run": [],
                "success": False,
                "blocker": str(e),
            }

        if isinstance(result, dict):
            return result
        return {
            "summary": str(result) if result else "Task completed.",
            "files_written": [],
            "commands_run": [],
            "success": True,
            "blocker": None,
        }
