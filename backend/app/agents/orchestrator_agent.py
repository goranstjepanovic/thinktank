"""
Phase 3 Orchestrator Agent — multi-agent mode.

The orchestrator reads the PRD, plans tasks autonomously, and delegates batches of
tasks to SubAgents that do the actual file writing and command execution.

Flow:
  1. (Caller generates PRD first, passes content in)
  2. Orchestrator loop: inspect project state → decide next batch of tasks (1-3) →
     run all tasks in the batch concurrently → repeat
  3. Sub-agents write files and run commands, reporting back via callbacks
  4. Orchestrator incorporates results, decides next batch or finishes
  5. If orchestrator needs user input, it sets user_message and yields to the caller;
     the caller suspends the session (WAITING) and resumes when a message arrives.
  6. User messages sent at any time are drained at the start of each round and
     surfaced to the orchestrator as context (non-blocking feedback path).
"""

import asyncio
import logging
from pathlib import Path
from typing import Callable, Awaitable

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import AsyncSessionLocal
from app.db.models import Idea, Phase3Session, SolutionBranch
from app.inference.base import Message
from app.inference.client import InferenceClient, INSPECT_FILES_TOOL
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
        "You may call `list_files`, `inspect_files`, and `grep_files` to inspect the project. "
        "- `list_files`: list directory contents to understand project layout\n"
        "- `inspect_files`: delegate reading to a sub-agent that returns compact summaries of each file "
        "(what is implemented, what is complete, what is missing). Use this instead of reading files yourself. "
        "Pass up to 10 file paths at a time.\n"
        "- `grep_files`: search across files for a pattern\n"
        "Do NOT call `file_edit` or `run_shell` — those are reserved for the sub-agent.\n\n"
        "## Product Requirements Document\n\n"
        f"{prd_excerpt}\n\n"
        "## Output format\n\n"
        "After any tool use (or immediately), output a JSON object only — no prose:\n"
        '{"analysis": "what has been done and what remains", '
        '"next_tasks": ['
        '{"id": "snake_case_id", "title": "short title ≤ 60 chars", '
        '"instruction": "complete self-contained instructions — list all files to create, '
        'their purpose, cross-file dependencies, and commands to run"}'
        '], "done": false, "user_message": null}\n\n'
        "You may include 1–3 tasks in `next_tasks`. Tasks in the same batch run concurrently, "
        "so they MUST target completely independent files and modules — no two tasks in a batch "
        "may write to the same file.\n\n"
        "When all PRD sections are implemented:\n"
        '{"analysis": "all tasks complete", "next_tasks": [], "done": true, "user_message": null}\n\n'
        "When you need the user to make a decision before you can proceed:\n"
        '{"analysis": "...", "next_tasks": [], "done": false, '
        '"user_message": "the specific question or decision you need from the user"}\n\n'
        "## Task instruction guidelines\n\n"
        "The sub-agent receives only the `instruction` field and the output directory path. Make it:\n"
        "- Fully self-contained: list every file by path with its purpose\n"
        "- Tech-stack specific: name libraries, exact versions, and patterns to use\n"
        "- Context-aware: tell the sub-agent which existing files to read for context first\n"
        "- Command-aware: list any post-write commands (install, build, test) to run\n"
        f"- Shell environment: {shell_environment_context()}\n\n"
        "## Project structure rules\n\n"
        "- There must be exactly ONE app structure with ONE build tool at the project root. "
        "Never create both a root app and a nested sub-app with a competing build tool "
        "(e.g. root Rollup + nested Vite, or two separate package.json trees that depend on each other).\n"
        "- If a framework demands its own directory layout (SvelteKit, Next.js, Nuxt), that layout IS the project root.\n"
        "- All package.json / pyproject.toml files must be consistent: same framework, same build tool.\n"
        "- HTML entry points must match the actual build output: Vite apps use `<script type=\"module\" src=\"/src/main.*\">`, "
        "not legacy `/build/bundle.js` references.\n"
        "- package.json `scripts` paths must be relative to that package.json's own directory.\n\n"
        "## Package version rules\n\n"
        "- All packages in a package.json must be mutually compatible — check peer dependency requirements.\n"
        "- Key compatibility constraints to enforce:\n"
        "  • Svelte 4 → @sveltejs/vite-plugin-svelte ^2\n"
        "  • Svelte 5 → @sveltejs/vite-plugin-svelte ^3 (NOT ^1 or ^2)\n"
        "  • Svelte 5 component mounting → use `mount(App, {target: ...})` from 'svelte', NOT `new App({target: ...})`\n"
        "  • Vite projects → no Rollup config at root; Vite IS the bundler\n"
        "  • SvelteKit → use @sveltejs/kit, not bare vite-plugin-svelte directly\n"
        "- When writing a task instruction that involves package.json, explicitly state the exact compatible versions to use.\n\n"
        "## Rules\n\n"
        "- Delegate 1–3 cohesive, independent tasks per response\n"
        "- Before choosing tasks, call `list_files` to see what is on disk, then `inspect_files` on relevant files\n"
        "- Tasks in the same batch must NOT overlap: each should own its own set of files\n"
        "- Track which PRD sections have been implemented and ensure full coverage\n"
        "- Set `done=true` only when all PRD sections are covered by completed tasks\n"
        "- Output ONLY the JSON object — no markdown fences, no extra text\n"
        "- NEVER create sub-agent tasks for file listing, reading, or inspection — "
        "use `list_files`, `inspect_files`, and `grep_files` directly yourself. "
        "Sub-agent tasks are ONLY for writing files and running commands.\n"
    )


def _orchestrator_user_prompt(
    idea: Idea,
    branch: SolutionBranch,
    completed_tasks: list[dict],
    follow_up_message: str | None = None,
    pending_user_messages: list[str] | None = None,
    verify_prd: bool = False,
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

    if follow_up_message:
        start_hint = (
            f"## User follow-up request\n\n"
            f"{follow_up_message}\n\n"
            "Call `list_files` to inspect the current project state, then plan and delegate tasks "
            "that address the user's request. Set `done=true` only when the request is fully implemented."
        )
    elif not completed_tasks:
        start_hint = (
            "No tasks have been started yet. "
            "Begin with the project scaffold: root files (README.md, .gitignore, .env.example, pyproject.toml / package.json), "
            "then proceed layer by layer."
        )
    else:
        start_hint = "Call `list_files` to check what is on disk, then decide the next task."

    feedback_block = ""
    if pending_user_messages:
        msgs = "\n".join(f"- {m}" for m in pending_user_messages)
        feedback_block = f"\n\n## User feedback (received while last batch was running)\n\n{msgs}\n\nIncorporate this feedback into your next task decisions."

    verify_block = ""
    if verify_prd:
        verify_block = (
            "\n\n## PRD Verification Required\n\n"
            "You indicated the implementation is complete. Before setting `done=true` again, "
            "you MUST run through ALL of the following checks:\n\n"
            "1. Call `list_files` to get the current project file tree\n"
            "2. Call `inspect_files` on key implementation files\n"
            "3. For each PRD section, confirm it is covered by what was built\n"
            "4. Check for structural problems:\n"
            "   - Only ONE build tool / package.json tree at the project root (no competing nested apps)\n"
            "   - HTML entry points reference the correct build output for the chosen bundler\n"
            "   - package.json scripts use paths relative to their own directory\n"
            "   - All packages are version-compatible (check framework + plugin peer deps)\n"
            "5. Spawn a build verification task: run the project's build command "
            "(e.g. `npm run build`, `pip install -e .`, `cargo build`) and confirm it exits 0 with no errors. "
            "If it fails, add repair tasks and set `done=false`.\n"
            "6. If any PRD section is missing, incomplete, or the build fails, add tasks to fix it.\n"
            "7. Only set `done=true` once ALL sections are covered AND the build succeeds.\n\n"
            "Do not skip this check — an implementation that does not build is not done."
        )

    return (
        f"PROJECT: {idea.name}\n"
        f"DESCRIPTION: {idea.description}\n\n"
        f"SELECTED APPROACH: {branch.approach_summary or 'N/A'}\n"
        f"{history_block}"
        f"{feedback_block}\n\n"
        f"{start_hint}"
        f"{verify_block}"
    )


def _inspector_system_prompt() -> str:
    return (
        "You are a code inspector sub-agent. Read the specified files and return concise structured summaries.\n\n"
        "For each file, report:\n"
        "- What it implements (classes, functions, endpoints, schemas, etc.)\n"
        "- What appears complete vs. stub/placeholder\n"
        "- What is likely missing based on typical patterns for this file type\n\n"
        "Output JSON only — no prose, no fences:\n"
        '{"files": [{"path": "...", "language": "...", "summary": "one-sentence overview", '
        '"implemented": ["item1", "item2"], "missing_or_incomplete": ["gap1", "gap2"]}]}'
    )


def _inspector_user_prompt(paths: list[str], focus: str, output_dir: str) -> str:
    paths_list = "\n".join(f"- {p}" for p in paths)
    focus_line = f"\nFocus on: {focus}" if focus else ""
    return (
        f"OUTPUT DIRECTORY: {output_dir}\n\n"
        f"Inspect these files:{focus_line}\n{paths_list}\n\n"
        "For each file: call read_file, then include it in the JSON summary. "
        "Keep each summary concise — 1 sentence overview, key items only."
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
        "- All paths passed to `file_edit` must be relative to the OUTPUT DIRECTORY — never prefix them with the project name or any parent folder\n"
        "- Parent directories are created automatically; never use mkdir\n"
        "- For binary files, skip them and note it in your summary\n"
        "- Use `delete_path` to remove deprecated or unused files/directories — do not leave dead code\n\n"
        "## Package and dependency rules\n\n"
        "- Use only ONE build tool / package manager at the project root — never create a second package.json tree "
        "that depends on the first (no nested app-within-app structures)\n"
        "- All packages in package.json must be mutually compatible. Check peer dependency requirements:\n"
        "  • Svelte 4 → @sveltejs/vite-plugin-svelte ^2\n"
        "  • Svelte 5 → @sveltejs/vite-plugin-svelte ^3 (NOT ^1 or ^2)\n"
        "  • Svelte 5 → mount(App, {target}) from 'svelte', NOT new App({target})\n"
        "  • Vite projects → HTML entry point must use <script type=\"module\" src=\"/src/main.*\">, "
        "NOT legacy /build/bundle.js or /build/bundle.css\n"
        "  • package.json scripts paths must be relative to that file's own directory\n"
        "- Before writing package.json, read any existing package.json to stay consistent with the chosen stack\n\n"
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
        follow_up_message: str | None = None,
    ) -> str:
        output_dir = session.output_dir or ""
        completed_tasks: list[dict] = []
        # Only inject follow-up context on the first round
        _initial_follow_up = follow_up_message
        _verification_pending = False  # True after first done=true; forces one explicit PRD check round

        for round_idx in range(_MAX_ORCHESTRATOR_ROUNDS):
            logger.info("orchestrator: round %d/%d", round_idx + 1, _MAX_ORCHESTRATOR_ROUNDS)
            await on_orchestrator_event("orchestrator_thinking", {})

            # Drain any user messages sent while the last batch was running (non-blocking)
            pending_messages: list[str] = []
            while True:
                try:
                    pending_messages.append(user_message_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if pending_messages:
                logger.info("orchestrator: %d pending user message(s) injected into round %d",
                            len(pending_messages), round_idx + 1)
                for m in pending_messages:
                    await on_orchestrator_event("orchestrator_message", {"content": f"[User]: {m}"})

            # Build orchestrator messages with accumulated context
            orch_messages = [
                Message(role="system", content=_orchestrator_system_prompt(prd_content)),
                Message(role="user", content=_orchestrator_user_prompt(
                    idea, branch, completed_tasks, _initial_follow_up,
                    pending_user_messages=pending_messages or None,
                    verify_prd=_verification_pending,
                )),
            ]
            _initial_follow_up = None  # only include on first round

            async def _orch_tool_cb(tool: str, result: dict) -> None:
                await on_orchestrator_event("orchestrator_tool", {"tool": tool, "result": result})

            async def _handle_inspect_files(args: dict) -> dict:
                paths = [str(p) for p in (args.get("paths") or []) if p][:10]
                focus = str(args.get("focus") or "")
                if not paths:
                    return {"files": [], "error": "no paths provided"}
                return await self._run_inspector_agent(
                    db, idea, branch, output_dir, paths, focus, on_orchestrator_event, round_idx
                )

            try:
                orch_result = await self._client.call_with_tools(
                    stage_key="phase3_orchestrator",
                    messages=orch_messages,
                    session=db,
                    idea_id=idea.id,
                    branch_id=branch.id,
                    allowed_file_dir=output_dir,
                    explore_only=True,
                    max_tool_rounds=12,
                    return_json=True,
                    call_index=round_idx * 100,
                    on_tool_result=_orch_tool_cb,
                    extra_tools=[INSPECT_FILES_TOOL],
                    custom_tool_handlers={"inspect_files": _handle_inspect_files},
                )
            except Exception as e:
                logger.error("orchestrator: round %d failed: %s", round_idx + 1, e)
                break

            if not isinstance(orch_result, dict):
                logger.warning("orchestrator: non-dict result in round %d", round_idx + 1)
                break

            user_message = orch_result.get("user_message")
            done = bool(orch_result.get("done", False))

            # Accept both next_tasks (new) and next_task (legacy single-task format)
            raw_tasks = orch_result.get("next_tasks")
            if not raw_tasks and orch_result.get("next_task"):
                raw_tasks = [orch_result["next_task"]]
            next_tasks: list[dict] = [t for t in (raw_tasks or []) if isinstance(t, dict)]

            logger.info("orchestrator: done=%s tasks=%d question=%s",
                        done, len(next_tasks), bool(user_message))

            # Orchestrator needs blocking user input before it can continue
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

            if done or not next_tasks:
                if done and not _verification_pending:
                    # First time done=true: force an explicit PRD verification round
                    _verification_pending = True
                    logger.info("orchestrator: done=true signalled — starting PRD verification round")
                    continue
                logger.info("orchestrator: signalled done after %d round(s)", round_idx + 1)
                break

            # Validate tasks
            valid_tasks = []
            for t in next_tasks[:3]:  # cap at 3 concurrent sub-agents
                instruction = str(t.get("instruction") or "").strip()
                if instruction:
                    valid_tasks.append(t)
                else:
                    logger.warning("orchestrator: task %r has empty instruction — skipping", t.get("id"))
            if not valid_tasks:
                logger.warning("orchestrator: no valid tasks in round %d — stopping", round_idx + 1)
                break

            # Emit started events for all tasks upfront
            for t in valid_tasks:
                task_id = str(t.get("id") or f"task_{round_idx}")
                task_title = str(t.get("title") or "Task")[:80]
                t["_id"] = task_id
                t["_title"] = task_title
                await on_orchestrator_event("sub_agent_started", {"task_id": task_id, "title": task_title})

            # Run all tasks in this batch concurrently
            batch_results = await self._run_task_batch(
                idea, branch, output_dir, valid_tasks,
                on_tool_result, on_orchestrator_event,
            )

            for t, sub_result in zip(valid_tasks, batch_results):
                task_id = t["_id"]
                task_title = t["_title"]
                await on_orchestrator_event("sub_agent_complete", {
                    "task_id": task_id,
                    "title": task_title,
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

    async def _run_task_batch(
        self,
        idea: Idea,
        branch: SolutionBranch,
        output_dir: str,
        tasks: list[dict],
        on_tool_result: OnToolResult,
        on_orchestrator_event: OnOrchestratorEvent,
    ) -> list[dict]:
        """Run a batch of tasks concurrently. Each task gets its own DB session."""

        async def _run_one(t: dict) -> dict:
            task_id = t["_id"]
            task_title = t["_title"]
            instruction = str(t.get("instruction") or "").strip()
            try:
                return await self._run_sub_agent(
                    idea, branch, output_dir,
                    task_id, task_title, instruction,
                    on_tool_result, on_orchestrator_event,
                )
            except asyncio.CancelledError:
                await on_orchestrator_event("sub_agent_complete", {
                    "task_id": task_id,
                    "title": task_title,
                    "summary": "Cancelled by user",
                    "files_written": [],
                    "commands_run": [],
                    "success": False,
                    "blocker": "Cancelled by user",
                })
                raise

        if len(tasks) == 1:
            return [await _run_one(tasks[0])]

        results = await asyncio.gather(*[_run_one(t) for t in tasks], return_exceptions=True)
        out: list[dict] = []
        for t, r in zip(tasks, results):
            if isinstance(r, BaseException):
                if isinstance(r, asyncio.CancelledError):
                    raise r
                logger.error("sub_agent: task '%s' raised: %s", t["_id"], r)
                out.append({
                    "summary": f"Task failed: {r}",
                    "files_written": [],
                    "commands_run": [],
                    "success": False,
                    "blocker": str(r),
                })
            else:
                out.append(r)
        return out

    async def _run_inspector_agent(
        self,
        db: AsyncSession,
        idea: Idea,
        branch: SolutionBranch,
        output_dir: str,
        paths: list[str],
        focus: str,
        on_orchestrator_event: OnOrchestratorEvent,
        round_idx: int,
    ) -> dict:
        logger.info("inspector: reading %d file(s): %s", len(paths), paths)
        await on_orchestrator_event("orchestrator_tool", {"tool": "inspect_files", "result": {"paths": paths, "focus": focus}})

        try:
            result = await self._client.call_with_tools(
                stage_key="phase3_orchestrator",
                messages=[
                    Message(role="system", content=_inspector_system_prompt()),
                    Message(role="user", content=_inspector_user_prompt(paths, focus, output_dir)),
                ],
                session=db,
                idea_id=idea.id,
                branch_id=branch.id,
                allowed_file_dir=output_dir,
                explore_only=True,
                max_tool_rounds=len(paths) + 2,
                return_json=True,
                call_index=round_idx * 100 + 50,
            )
        except Exception as e:
            logger.error("inspector: failed: %s", e)
            return {"files": [{"path": p, "summary": f"failed to inspect: {e}", "implemented": [], "missing_or_incomplete": []} for p in paths]}

        if isinstance(result, dict) and "files" in result:
            return result
        return {"files": [{"path": p, "summary": "inspection returned unexpected format", "implemented": [], "missing_or_incomplete": []} for p in paths]}

    async def _run_sub_agent(
        self,
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
            await on_tool_result(tool_name, result)
            if tool_name == "file_edit":
                detail = result.get("path", "")
            elif tool_name == "delete_path":
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

        stage_cfg = self._client._registry.get_stage("phase3_sub_agent")
        models_to_try: list[str | None] = [None] + list(stage_cfg.fallback_models)
        last_result: dict = {}

        for attempt, model_override in enumerate(models_to_try):
            if model_override:
                logger.info("sub_agent: task '%s' — fallback attempt %d with model %s", task_title, attempt, model_override)
                await on_orchestrator_event("sub_agent_model_fallback", {
                    "task_id": task_id,
                    "model": model_override,
                    "attempt": attempt,
                })

            try:
                async with AsyncSessionLocal() as sub_db:
                    last_result = await self._client.call_with_tools(
                        stage_key="phase3_sub_agent",
                        messages=[
                            Message(role="system", content=_sub_agent_system_prompt()),
                            Message(role="user", content=_sub_agent_user_prompt(task_instruction, output_dir, idea.name)),
                        ],
                        session=sub_db,
                        idea_id=idea.id,
                        branch_id=branch.id,
                        allowed_file_dir=output_dir,
                        explore_only=False,
                        max_tool_rounds=60,
                        return_json=True,
                        call_index=0,
                        on_tool_result=_wrapped_on_tool,
                        model_override=model_override,
                    )
            except Exception as e:
                logger.error("sub_agent: task '%s' attempt %d failed: %s", task_title, attempt, e)
                last_result = {
                    "summary": f"Task failed with error: {e}",
                    "files_written": [],
                    "commands_run": [],
                    "success": False,
                    "blocker": str(e),
                }

            if last_result.get("success", True):
                return last_result

            logger.info("sub_agent: task '%s' attempt %d unsuccessful (success=false), %s",
                        task_title, attempt,
                        f"retrying with {models_to_try[attempt + 1]}" if attempt + 1 < len(models_to_try) else "no more fallbacks")

        return last_result
