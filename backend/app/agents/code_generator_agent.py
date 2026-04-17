"""
Phase 3 Code Generator Agent — multi-pass approach.

Pass 0: JSON call → file plan ({files: [{path, description}], commands: [str]})
Pass 1-N: One call_text() per file → write immediately to disk → emit event
Final: Run each setup command → emit event
"""

import logging
import re
from pathlib import Path
from typing import Callable, Awaitable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, Idea, Phase2Session, Phase3Session, SolutionBranch
from app.inference.base import Message
from app.inference.client import InferenceClient
from app.services.file_manager import file_manager
from app.tools.shell_runner import run_shell_command

logger = logging.getLogger(__name__)

PLAN_STAGE_KEY = "phase3_plan"
FILE_STAGE_KEY = "phase3_file"

_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)\n?```\s*$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Remove a markdown code fence wrapper if present (e.g. ```json...```)."""
    m = _FENCE_RE.match(text.strip())
    return m.group(1) if m else text


def _plan_system_prompt() -> str:
    return (
        "You are an expert software architect. Given a project specification, produce a complete, "
        "well-structured file plan for the project.\n\n"
        "## Folder structure rules\n\n"
        "Organise files into directories that reflect the project's architecture. Use these conventions:\n"
        "- `frontend/` or `client/` — all UI code (React, Vue, HTML/CSS/JS, etc.)\n"
        "- `backend/` or `api/` — all server-side code (FastAPI, Express, Django, etc.)\n"
        "- `database/` or `db/` — migrations, seed data, schema files\n"
        "- `infra/` or `deploy/` — Dockerfiles, docker-compose, CI/CD, Kubernetes manifests\n"
        "- `docs/` — documentation beyond the README\n"
        "- `tests/` — if tests live outside the component they test\n"
        "For single-layer projects (e.g. a CLI tool, a pure backend API, a pure frontend) "
        "use a `src/` subdirectory instead of splitting by tier.\n\n"
        "## Root-level files (always include)\n\n"
        "- `README.md` — setup, usage, and architecture overview\n"
        "- `.gitignore` — appropriate for the tech stack\n"
        "- `.env.example` — all required environment variables with placeholder values\n"
        "- One of: `Makefile`, `docker-compose.yml`, or a root `package.json` / `pyproject.toml` "
        "with `dev`, `build`, `test` scripts — whichever fits the stack\n"
        "- `docker-compose.yml` if the project has more than one service (frontend + backend, "
        "app + database, etc.)\n\n"
        "## Output format\n\n"
        "Output a JSON object with EXACTLY this structure:\n"
        '{"files": [{"path": "relative/path/to/file.ext", "description": "one-line purpose"}], '
        '"commands": ["shell command to set up and verify the project"]}\n\n'
        "Rules:\n"
        "- Every path must include at least one directory prefix — NO bare filenames at root "
        "except for the root-level files listed above\n"
        "- Order files: root scaffolding first, then by directory (backend before frontend), "
        "then tests, then docs\n"
        "- Descriptions must be specific (not just 'main file' — say what it does)\n"
        "- Commands: non-interactive only; install dependencies then verify the build/tests\n"
        "- Output ONLY the JSON object — no prose, no markdown fences\n"
    )


def _plan_user_prompt(
    idea: Idea,
    branch: SolutionBranch,
    resolution_summary: str,
    architecture_doc: str,
    component_specs: str,
    roadmap_doc: str,
) -> str:
    return (
        f"IDEA: {idea.name}\n"
        f"DESCRIPTION:\n{idea.description}\n\n"
        f"REQUIREMENTS:\n{idea.requirements}\n\n"
        f"CONSTRAINTS:\n{idea.constraints}\n\n"
        f"SELECTED SOLUTION (Branch {branch.branch_index}):\n"
        f"{branch.approach_summary or 'N/A'}\n\n"
        f"DECISIONS MADE (Phase 2 Q&A Summary):\n{resolution_summary}\n\n"
        f"ARCHITECTURE OVERVIEW:\n{architecture_doc}\n\n"
        f"COMPONENT SPECIFICATIONS:\n{component_specs}\n\n"
        f"IMPLEMENTATION ROADMAP:\n{roadmap_doc}\n\n"
        "Analyse the architecture above and determine the project type "
        "(e.g. full-stack web app, REST API + frontend, CLI tool, library, etc.). "
        "Then produce the JSON file plan with a proper folder structure as instructed. "
        "Ensure every component has its own directory and the root contains only "
        "cross-cutting files (README, .gitignore, .env.example, Makefile / docker-compose)."
    )


def _file_system_prompt() -> str:
    return (
        "You are an expert software developer. Write the complete content of a single source file.\n\n"
        "Output ONLY the raw file content — no markdown code fences, no explanations, no preamble. "
        "The output will be written directly to disk exactly as you produce it."
    )


def _file_user_prompt(
    idea: Idea,
    branch: SolutionBranch,
    resolution_summary: str,
    architecture_doc: str,
    file_plan_summary: str,
    written_paths: list[str],
    path: str,
    description: str,
) -> str:
    already_written = "\n".join(f"  - {p}" for p in written_paths) if written_paths else "  (none yet)"
    return (
        f"PROJECT: {idea.name}\n"
        f"DESCRIPTION: {idea.description}\n\n"
        f"REQUIREMENTS:\n{idea.requirements}\n\n"
        f"CONSTRAINTS:\n{idea.constraints}\n\n"
        f"SOLUTION APPROACH:\n{branch.approach_summary or 'N/A'}\n\n"
        f"DECISIONS MADE:\n{resolution_summary}\n\n"
        f"ARCHITECTURE:\n{architecture_doc}\n\n"
        f"ALL FILES IN THIS PROJECT:\n{file_plan_summary}\n\n"
        f"FILES ALREADY WRITTEN:\n{already_written}\n\n"
        f"WRITE THIS FILE: {path}\n"
        f"PURPOSE: {description}\n\n"
        f"Write the complete content of `{path}` now."
    )


def _iteration_plan_system_prompt() -> str:
    return (
        "You are an expert software developer. Given an existing project and a user change request, "
        "produce a list of ONLY the files that need to be created or modified.\n\n"
        "Output a JSON object:\n"
        '{"files": [{"path": "relative/path", "description": "what to do in this file"}], '
        '"commands": ["optional post-change command"]}\n\n'
        "Rules:\n"
        "- Only include files that actually need to change — do NOT list unchanged files\n"
        "- New files must follow the existing project folder structure "
        "(e.g. place new backend code in backend/, frontend code in frontend/)\n"
        "- If creating a new file, state 'CREATE:' at the start of its description\n"
        "- Output ONLY the JSON object — no prose, no markdown fences\n"
    )


def _iteration_plan_user_prompt(
    idea: Idea,
    branch: SolutionBranch,
    user_request: str,
    existing_files: list[str],
    previous_summary: str,
) -> str:
    file_tree = "\n".join(f"  {p}" for p in existing_files) if existing_files else "  (none)"
    return (
        f"PROJECT: {idea.name}\n"
        f"DESCRIPTION: {idea.description}\n\n"
        f"PREVIOUS BUILD SUMMARY:\n{previous_summary}\n\n"
        f"EXISTING FILES:\n{file_tree}\n\n"
        f"USER REQUEST:\n{user_request}\n\n"
        "List only the files that need to be created or modified to fulfil this request."
    )


def _format_file_plan(files: list[dict]) -> str:
    return "\n".join(f"  {f['path']} — {f['description']}" for f in files)


class CodeGeneratorAgent:
    def __init__(self, inference_client: InferenceClient) -> None:
        self._client = inference_client

    async def run_implementation(
        self,
        db: AsyncSession,
        session: Phase3Session,
        idea: Idea,
        branch: SolutionBranch,
        on_tool_result: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> str:
        resolution_summary = await self._load_resolution_summary(db, idea)
        architecture_doc = await self._load_doc(db, branch, "ARCHITECTURE_OVERVIEW")
        component_specs = await self._load_doc(db, branch, "COMPONENT_SPECS")
        roadmap_doc = await self._load_doc(db, branch, "IMPLEMENTATION_ROADMAP")
        output_dir = session.output_dir or ""

        # ── Pass 0: get file plan ─────────────────────────────────────────────
        logger.info("code_generator: requesting file plan for idea=%s branch=%s", idea.id, branch.id)
        plan_messages = [
            Message(role="system", content=_plan_system_prompt()),
            Message(role="user", content=_plan_user_prompt(
                idea, branch, resolution_summary,
                architecture_doc, component_specs, roadmap_doc,
            )),
        ]
        plan_data = await self._client.call(
            stage_key=PLAN_STAGE_KEY,
            messages=plan_messages,
            session=db,
            idea_id=idea.id,
            branch_id=branch.id,
            call_type="PHASE3",
            call_index=0,
        )

        files: list[dict] = plan_data.get("files", [])
        commands: list[str] = plan_data.get("commands", [])

        if not files:
            logger.warning("code_generator: file plan produced no files")
            return (
                "The model did not produce a file plan. "
                "Review the audit log and try again."
            )

        logger.info("code_generator: file plan has %d files, %d commands", len(files), len(commands))
        if on_tool_result:
            await on_tool_result("plan_ready", {"file_count": len(files), "files": files, "commands": commands})

        # ── Pass 1-N: generate each file ─────────────────────────────────────
        file_plan_summary = _format_file_plan(files)
        written_paths: list[str] = []
        files_written = 0

        for i, file_spec in enumerate(files):
            path = file_spec.get("path", "").strip()
            description = file_spec.get("description", "")
            if not path:
                continue

            logger.info("code_generator: generating file %d/%d: %s", i + 1, len(files), path)
            if on_tool_result:
                await on_tool_result("pass_started", {
                    "file_path": path,
                    "file_index": i,
                    "total_files": len(files),
                })

            file_messages = [
                Message(role="system", content=_file_system_prompt()),
                Message(role="user", content=_file_user_prompt(
                    idea, branch, resolution_summary, architecture_doc,
                    file_plan_summary, written_paths, path, description,
                )),
            ]
            try:
                raw = await self._client.call_text(
                    stage_key=FILE_STAGE_KEY,
                    messages=file_messages,
                    session=db,
                    idea_id=idea.id,
                    branch_id=branch.id,
                    call_type="PHASE3",
                    call_index=i + 1,
                )
                content = _strip_code_fence(raw)
            except Exception as e:
                logger.error("code_generator: failed to generate %s: %s", path, e)
                continue

            abs_path = str(Path(output_dir) / path)
            result = await file_manager.write_file(abs_path, content)
            size_bytes = len(content.encode("utf-8"))

            if result.get("success"):
                files_written += 1
                written_paths.append(path)
                logger.debug("code_generator: wrote %s (%d B)", path, size_bytes)
            else:
                logger.error("code_generator: failed to write %s: %s", path, result.get("error"))

            if on_tool_result:
                await on_tool_result("file_edit", {
                    "path": path,
                    "operation": "write_file",
                    "success": result.get("success", False),
                    "size_bytes": size_bytes,
                    "detail": result.get("error", ""),
                })

        # ── Final: run setup commands ─────────────────────────────────────────
        for command in commands:
            logger.info("code_generator: running command: %s", command)
            shell_result = await run_shell_command(command=command, working_dir=output_dir)
            if on_tool_result:
                await on_tool_result("run_shell", {
                    "command": command,
                    "exit_code": shell_result.exit_code,
                    "stdout": shell_result.stdout,
                    "stderr": shell_result.stderr,
                    "timed_out": shell_result.timed_out,
                    "duration_ms": shell_result.duration_ms,
                })

        file_list = ", ".join(written_paths[:5])
        if len(written_paths) > 5:
            file_list += f", … ({len(written_paths) - 5} more)"
        cmd_summary = f" Ran {len(commands)} setup command(s)." if commands else ""
        return (
            f"Wrote {files_written}/{len(files)} file(s): {file_list}.{cmd_summary} "
            f"Project is at: {output_dir}"
        )

    async def _load_resolution_summary(self, db: AsyncSession, idea: Idea) -> str:
        result = await db.execute(
            select(Phase2Session).where(Phase2Session.idea_id == idea.id)
        )
        phase2 = result.scalar_one_or_none()
        if phase2 and phase2.resolution_summary:
            return phase2.resolution_summary
        return "No resolution summary available — use the idea requirements and constraints."

    async def run_iteration(
        self,
        db: AsyncSession,
        session: Phase3Session,
        idea: Idea,
        branch: SolutionBranch,
        user_request: str,
        on_tool_result: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> str:
        """
        Iteration pass: given a user change request, produce only the affected files.
        Returns a plain-text summary of what changed.
        """
        output_dir = session.output_dir or ""

        # Collect existing file paths for context
        existing_files: list[str] = []
        if output_dir and Path(output_dir).is_dir():
            base = Path(output_dir)
            existing_files = sorted(
                str(p.relative_to(base)).replace("\\", "/")
                for p in base.rglob("*") if p.is_file()
            )

        previous_summary = session.summary or "No previous summary."

        # Pass 0: plan which files to change
        plan_messages = [
            Message(role="system", content=_iteration_plan_system_prompt()),
            Message(role="user", content=_iteration_plan_user_prompt(
                idea, branch, user_request, existing_files, previous_summary,
            )),
        ]
        plan_data = await self._client.call(
            stage_key="phase3_iteration",
            messages=plan_messages,
            session=db,
            idea_id=idea.id,
            branch_id=branch.id,
            call_type="PHASE3_ITER",
            call_index=0,
        )

        files: list[dict] = plan_data.get("files", [])
        commands: list[str] = plan_data.get("commands", [])

        if not files:
            return "No files needed to change for this request."

        if on_tool_result:
            await on_tool_result("plan_ready", {"file_count": len(files), "files": files, "commands": commands})

        # Pass 1-N: generate each file
        file_plan_summary = _format_file_plan(files)
        written_paths: list[str] = []
        files_written = 0

        for i, file_spec in enumerate(files):
            path = file_spec.get("path", "").strip()
            description = file_spec.get("description", "")
            if not path:
                continue

            if on_tool_result:
                await on_tool_result("pass_started", {
                    "file_path": path,
                    "file_index": i,
                    "total_files": len(files),
                })

            file_messages = [
                Message(role="system", content=_file_system_prompt()),
                Message(role="user", content=_file_user_prompt(
                    idea, branch,
                    f"User request: {user_request}",
                    f"Existing files: {', '.join(existing_files[:20])}",
                    file_plan_summary, written_paths, path, description,
                )),
            ]
            try:
                raw = await self._client.call_text(
                    stage_key=FILE_STAGE_KEY,
                    messages=file_messages,
                    session=db,
                    idea_id=idea.id,
                    branch_id=branch.id,
                    call_type="PHASE3_ITER",
                    call_index=i + 1,
                )
                content = _strip_code_fence(raw)
            except Exception as e:
                logger.error("iteration: failed to generate %s: %s", path, e)
                continue

            abs_path = str(Path(output_dir) / path)
            result = await file_manager.write_file(abs_path, content)
            size_bytes = len(content.encode("utf-8"))

            if result.get("success"):
                files_written += 1
                written_paths.append(path)

            if on_tool_result:
                await on_tool_result("file_edit", {
                    "path": path,
                    "operation": "write_file",
                    "success": result.get("success", False),
                    "size_bytes": size_bytes,
                    "detail": result.get("error", ""),
                })

        for command in commands:
            shell_result = await run_shell_command(command=command, working_dir=output_dir)
            if on_tool_result:
                await on_tool_result("run_shell", {
                    "command": command,
                    "exit_code": shell_result.exit_code,
                    "stdout": shell_result.stdout,
                    "stderr": shell_result.stderr,
                    "timed_out": shell_result.timed_out,
                    "duration_ms": shell_result.duration_ms,
                })

        changed = ", ".join(written_paths[:5])
        if len(written_paths) > 5:
            changed += f", … ({len(written_paths) - 5} more)"
        return f"Updated {files_written} file(s): {changed}." if files_written else "No files were written."

    async def _load_doc(self, db: AsyncSession, branch: SolutionBranch, doc_type: str) -> str:
        result = await db.execute(
            select(Document).where(
                Document.branch_id == branch.id,
                Document.doc_type == doc_type,
            )
        )
        doc = result.scalar_one_or_none()
        if doc is None:
            return f"(No {doc_type.lower().replace('_', ' ')} document available)"
        try:
            return Path(doc.file_path).read_text(encoding="utf-8")
        except Exception:
            return f"(Could not load {doc_type} document)"
