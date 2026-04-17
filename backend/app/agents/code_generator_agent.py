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
PRD_STAGE_KEY  = "phase3_prd"
PRD_PATH       = "docs/PRD.md"

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
        '{"message": "2-3 sentence conversational overview: what you are building, the tech stack chosen, and how you have structured it", '
        '"files": [{"path": "relative/path/to/file.ext", "description": "one-line purpose"}], '
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


def _prd_system_prompt() -> str:
    return (
        "You are a technical writer and software architect. "
        "Write a comprehensive Product Requirements Document (PRD) in Markdown.\n\n"
        "This file will live at `docs/PRD.md` in the generated project and serves as the "
        "single source of truth for anyone continuing development — including other AI coding tools.\n\n"
        "## Required sections\n\n"
        "1. **Project Overview** — what the project is, the problem it solves, who it is for\n"
        "2. **Requirements** — functional and non-functional requirements, listed clearly\n"
        "3. **Constraints** — technical, resource, and business constraints\n"
        "4. **Solution Architecture** — chosen approach and key design decisions\n"
        "5. **Components** — each component, its responsibility, and its main interfaces\n"
        "6. **Implementation Roadmap** — phases, milestones, and task breakdown\n"
        "7. **Technical Decisions** — decisions made during design/Q&A and their rationale\n"
        "8. **Project Structure** — directory layout with a brief description of each folder\n"
        "9. **Setup & Development** — how to install dependencies, run the project, and run tests\n\n"
        "Write for a developer who has never seen this project before. "
        "Output ONLY raw Markdown — no code fences wrapping the document, no preamble."
    )


def _prd_user_prompt(
    idea: Idea,
    branch: SolutionBranch,
    resolution_summary: str,
    architecture_doc: str,
    component_specs: str,
    roadmap_doc: str,
    file_plan_summary: str,
) -> str:
    return (
        f"PROJECT: {idea.name}\n"
        f"DESCRIPTION:\n{idea.description}\n\n"
        f"REQUIREMENTS:\n{idea.requirements}\n\n"
        f"CONSTRAINTS:\n{idea.constraints}\n\n"
        f"SELECTED SOLUTION (Branch {branch.branch_index}):\n"
        f"{branch.approach_summary or 'N/A'}\n\n"
        f"TECHNICAL DECISIONS (Phase 2 Q&A):\n{resolution_summary}\n\n"
        f"ARCHITECTURE:\n{architecture_doc}\n\n"
        f"COMPONENT SPECIFICATIONS:\n{component_specs}\n\n"
        f"IMPLEMENTATION ROADMAP:\n{roadmap_doc}\n\n"
        f"PROJECT FILE STRUCTURE:\n{file_plan_summary}\n\n"
        "Write the complete PRD now."
    )


_MAX_FILE_BYTES = 10_000      # max bytes included per file
_MAX_TOTAL_BYTES = 80_000    # max total file content in the prompt


def _read_project_files(output_dir: str) -> dict[str, str]:
    """
    Read project files and return {rel_path: content}.
    Files over _MAX_FILE_BYTES are truncated; total is capped at _MAX_TOTAL_BYTES.
    Binary files are skipped.
    """
    base = Path(output_dir)
    if not base.is_dir():
        return {}

    _SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".nuxt"}
    contents: dict[str, str] = {}
    total = 0

    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        # Skip directories we never want to read
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if total >= _MAX_TOTAL_BYTES:
            break
        rel = str(p.relative_to(base)).replace("\\", "/")
        try:
            raw = p.read_bytes()
            # Skip binary files
            if b"\x00" in raw[:512]:
                continue
            text = raw[:_MAX_FILE_BYTES].decode("utf-8", errors="replace")
            truncated = len(raw) > _MAX_FILE_BYTES
            contents[rel] = text + ("\n... (truncated)" if truncated else "")
            total += len(text)
        except Exception:
            continue

    return contents


def _format_file_contents(contents: dict[str, str]) -> str:
    if not contents:
        return "  (no files read)"
    parts = []
    for path, text in contents.items():
        parts.append(f"--- {path} ---\n{text}")
    return "\n\n".join(parts)


def _iteration_plan_system_prompt() -> str:
    return (
        "You are an expert software developer. Given an existing project and a user change request, "
        "use the available tools to explore the project structure, read relevant files, and search for "
        "specific code. Once you understand what needs to change, output a JSON plan.\n\n"
        "## Exploration workflow\n\n"
        "1. Call `list_files` with path='' to see the project root layout\n"
        "2. Call `list_files` on subdirectories you need to explore\n"
        "3. Call `grep_files` to find the exact file containing a function, import, or variable\n"
        "4. Call `read_file` to inspect the files that need to change\n"
        "5. Once you have enough context, output the JSON plan\n\n"
        "## Output format\n\n"
        "Output a JSON object — no prose, no markdown fences:\n"
        '{"message": "2-3 sentences describing what you found in the project and exactly what changes you will make", '
        '"files": [{"path": "relative/path", "description": "what to do in this file"}], '
        '"commands": ["optional post-change shell command"]}\n\n'
        "Rules:\n"
        "- Only include files that actually need to change — do NOT list unchanged files\n"
        "- New files must follow the existing project folder structure\n"
        "- If creating a new file, state 'CREATE:' at the start of its description\n"
        "- The description must be specific about exactly what change is needed\n"
    )


def _iteration_plan_user_prompt(
    idea: Idea,
    branch: SolutionBranch,
    user_request: str,
    previous_summary: str,
) -> str:
    return (
        f"PROJECT: {idea.name}\n"
        f"DESCRIPTION: {idea.description}\n\n"
        f"PREVIOUS BUILD SUMMARY:\n{previous_summary}\n\n"
        f"USER REQUEST:\n{user_request}\n\n"
        "Use the available tools to explore the project, locate the relevant files, and then "
        "output only the files that need to be created or modified to fulfil the request."
    )


def _normalize_files(raw: list) -> list[dict]:
    """Model sometimes returns strings instead of {path, description} dicts — normalise both."""
    result = []
    for item in raw:
        if isinstance(item, str):
            result.append({"path": item.strip(), "description": ""})
        elif isinstance(item, dict):
            result.append({"path": str(item.get("path", "") or "").strip(), "description": str(item.get("description", "") or "")})
    return [f for f in result if f["path"]]


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

        files: list[dict] = _normalize_files(plan_data.get("files", []))
        commands: list[str] = [str(c) for c in plan_data.get("commands", []) if c]

        if not files:
            logger.warning("code_generator: file plan produced no files")
            return (
                "The model did not produce a file plan. "
                "Review the audit log and try again."
            )

        # Always include docs/PRD.md as the first file; remove any model-planned duplicate
        files = [f for f in files if f.get("path", "").strip() != PRD_PATH]
        files.insert(0, {"path": PRD_PATH, "description": "Product Requirements Document — full project specification"})

        plan_message = str(plan_data.get("message", "")).strip()
        logger.info("code_generator: file plan has %d files (+PRD), %d commands", len(files) - 1, len(commands))
        if on_tool_result:
            await on_tool_result("plan_ready", {"file_count": len(files), "files": files, "commands": commands, "message": plan_message})

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

            if path == PRD_PATH:
                file_messages = [
                    Message(role="system", content=_prd_system_prompt()),
                    Message(role="user", content=_prd_user_prompt(
                        idea, branch, resolution_summary,
                        architecture_doc, component_specs, roadmap_doc,
                        file_plan_summary,
                    )),
                ]
                stage_key = PRD_STAGE_KEY
            else:
                file_messages = [
                    Message(role="system", content=_file_system_prompt()),
                    Message(role="user", content=_file_user_prompt(
                        idea, branch, resolution_summary, architecture_doc,
                        file_plan_summary, written_paths, path, description,
                    )),
                ]
                stage_key = FILE_STAGE_KEY

            try:
                raw = await self._client.call_text(
                    stage_key=stage_key,
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
        previous_summary = session.summary or "No previous summary."

        # Pass 0: explore project with tools, then plan which files to change
        plan_messages = [
            Message(role="system", content=_iteration_plan_system_prompt()),
            Message(role="user", content=_iteration_plan_user_prompt(
                idea, branch, user_request, previous_summary,
            )),
        ]
        plan_data = await self._client.call_with_tools(
            stage_key="phase3_explore",
            messages=plan_messages,
            session=db,
            idea_id=idea.id,
            branch_id=branch.id,
            allowed_file_dir=output_dir,
            explore_only=True,
            max_tool_rounds=10,
            return_json=True,
        )

        if not isinstance(plan_data, dict):
            logger.warning("iteration: explore stage returned non-dict: %s", type(plan_data))
            plan_data = {}
        files: list[dict] = _normalize_files(plan_data.get("files", []))
        commands: list[str] = [str(c) for c in plan_data.get("commands", []) if c]

        if not files:
            return "No files needed to change for this request."

        plan_message = str(plan_data.get("message", "")).strip()
        if on_tool_result:
            await on_tool_result("plan_ready", {"file_count": len(files), "files": files, "commands": commands, "message": plan_message})

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

            existing_content = ""
            try:
                _ep = Path(output_dir) / path
                if _ep.exists() and _ep.is_file():
                    _raw = _ep.read_bytes()
                    if b"\x00" not in _raw[:512]:
                        existing_content = _raw[:12_000].decode("utf-8", errors="replace")
                        if len(_raw) > 12_000:
                            existing_content += "\n... (truncated)"
            except Exception:
                pass
            existing_block = (
                f"CURRENT CONTENT OF {path}:\n{existing_content}\n\n"
                if existing_content else ""
            )
            file_messages = [
                Message(role="system", content=_file_system_prompt()),
                Message(role="user", content=(
                    f"PROJECT: {idea.name}\n\n"
                    f"USER REQUEST: {user_request}\n\n"
                    f"{existing_block}"
                    f"CHANGE REQUIRED: {description}\n\n"
                    f"Write the complete updated content of `{path}` now."
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
