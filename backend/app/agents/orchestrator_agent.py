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
import json
import logging
import platform
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Awaitable

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.engine import AsyncSessionLocal
from app.db.models import Idea, Phase3Session, SolutionBranch
from app.inference.base import Message
from app.inference.base import ToolDefinition
from app.inference.client import InferenceClient, INSPECT_FILES_TOOL, READ_PRD_TOOL
from app.tools.shell_runner import shell_environment_context, run_shell_command
from app.tools.interface_extractor import write_interface_manifest, format_manifest_summary, extract_interface

logger = logging.getLogger(__name__)

_MAX_ORCHESTRATOR_ROUNDS = 100  # safety ceiling — real stops are done=true or consecutive empty rounds

_THINK_TAG_OPEN = "<think>"
_THINK_TAG_CLOSE = "</think>"


def _extract_think_content(token: str, in_think: bool) -> tuple[bool, str]:
    """Extract only content inside <think>…</think> blocks for streaming display.

    Everything outside think blocks (tool_call JSON, final dispatch JSON) is discarded —
    only the model's reasoning text is forwarded to the UI.
    """
    think_parts: list[str] = []
    buf = token
    while buf:
        if in_think:
            end = buf.find(_THINK_TAG_CLOSE)
            if end == -1:
                think_parts.append(buf)  # still inside think block
                break
            think_parts.append(buf[:end])
            in_think = False
            buf = buf[end + len(_THINK_TAG_CLOSE):]
        else:
            start = buf.find(_THINK_TAG_OPEN)
            if start == -1:
                break  # outside think block — skip (tool_call / JSON dispatch)
            in_think = True
            buf = buf[start + len(_THINK_TAG_OPEN):]
    return in_think, "".join(think_parts)
_BUILD_CHECK_INTERVAL = 3  # run a build check every N implementation rounds

_RUN_BUILD_TOOL = ToolDefinition(
    name="run_build",
    description=(
        "Run the project build command in the output directory and return compiler/bundler output. "
        "Auto-detects the build tool from package.json, pyproject.toml, or Cargo.toml. "
        "Use this to catch import errors, missing exports, type mismatches, and syntax errors "
        "before declaring the implementation complete."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Override the auto-detected build command "
                    "(e.g. 'npm run build', 'npm run typecheck', 'cargo check'). "
                    "Omit to auto-detect from the project root."
                ),
            }
        },
        "required": [],
    },
)


_PLAN_LIST_TOOL = ToolDefinition(
    name="plan_list",
    description=(
        "List tasks in the current implementation plan. Returns tasks in order with their status. "
        "Supports pagination — use offset/limit to avoid loading all tasks at once on large plans."
    ),
    parameters={
        "type": "object",
        "properties": {
            "offset": {"type": "integer", "description": "Number of tasks to skip (default 0)"},
            "limit":  {"type": "integer", "description": "Max tasks to return (default 20, max 50)"},
        },
        "required": [],
    },
)

_PLAN_ADD_TOOL = ToolDefinition(
    name="plan_add",
    description=(
        "Add a new pending task to the plan. Each task must be scoped to exactly 1 file and one logical unit."
    ),
    parameters={
        "type": "object",
        "properties": {
            "id":          {"type": "string", "description": "Unique snake_case identifier"},
            "title":       {"type": "string", "description": "Short title ≤60 chars"},
            "instruction": {"type": "string", "description": "Complete self-contained task instruction"},
            "notes":       {"type": "string", "description": "Optional notes"},
        },
        "required": ["id", "title", "instruction"],
    },
)

_PLAN_UPDATE_TOOL = ToolDefinition(
    name="plan_update",
    description=(
        "Update a task's status, instruction, or notes. "
        "Call with status='in_progress' before dispatching, status='done' after success, "
        "status='failed' when permanently blocked, status='skipped' when the task is no longer "
        "needed (e.g. superseded by another task or a plan change)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "id":          {"type": "string", "description": "Task id to update"},
            "status":      {"type": "string", "enum": ["pending", "in_progress", "done", "failed", "skipped"]},
            "title":       {"type": "string", "description": "New title (optional)"},
            "instruction": {"type": "string", "description": "New instruction (optional)"},
            "notes":       {"type": "string", "description": "Notes to append (optional)"},
        },
        "required": ["id"],
    },
)

_PLAN_REMOVE_TOOL = ToolDefinition(
    name="plan_remove",
    description="Remove a task from the plan by id. Use when replacing a failed task with smaller ones.",
    parameters={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Task id to remove"},
        },
        "required": ["id"],
    },
)

_PLAN_NEXT_TOOL = ToolDefinition(
    name="plan_next",
    description=(
        "Get the next task to work on. Returns the first in_progress task if any, "
        "otherwise the first non-done task in array order (pending or failed). "
        "Failed tasks are flagged with retry=true and a warning — use a different approach. "
        "Returns null only when all tasks are done."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)


def _detect_tech_type(prd_content: str) -> str:
    """Heuristically detect the primary technology stack from PRD content."""
    text = prd_content.lower()
    if any(k in text for k in ("asp.net", " .net ", "c#", ".csproj", "dotnet restore", "dotnet new", "nuget")):
        return "dotnet"
    if any(k in text for k in ("fastapi", "django", "flask", "uvicorn", "pyproject.toml", "requirements.txt")):
        return "python"
    if any(k in text for k in ("express", "next.js", "nextjs", "node.js", "npm install", "package.json", "vite", "svelte", "vue", "react")):
        return "node"
    if any(k in text for k in ("go.mod", "golang", " goroutine", "go build")):
        return "go"
    if any(k in text for k in ("cargo.toml", "rust ", " crate")):
        return "rust"
    return ""


async def _research_framework(tech_type: str) -> str | None:
    """Run a single web search for current framework version and best practices. Returns formatted text or None."""
    from app.config import settings as _settings
    from app.tools.web_search import web_search as _web_search

    _QUERIES: dict[str, str] = {
        "dotnet": "ASP.NET Core .NET current LTS version 2025 dotnet new templates recommended",
        "python": "Python current stable version 2025 pip pyproject.toml FastAPI Django recommended",
        "node": "Node.js current LTS version 2025 npm TypeScript project setup recommended",
        "go": "Go current stable version 2025 modules recommended project structure",
        "rust": "Rust current stable version 2025 Cargo.toml recommended project setup",
    }
    query = _QUERIES.get(tech_type)
    if not query:
        return None
    tavily_key = getattr(_settings, "tavily_api_key", "") or ""
    try:
        result = await _web_search(query=query, tavily_api_key=tavily_key, max_results=3)
        if result.error or not result.results:
            logger.debug("framework research: no results for %s: %s", tech_type, result.error)
            return None
        snippets = "\n\n".join(
            f"**{h.title}**\n{h.snippet[:400]}"
            for h in result.results[:3]
        )
        return (
            f"## Framework Research — live data ({tech_type})\n\n"
            f"The following was retrieved from the web before implementation started. "
            f"Use it to pick the correct current version numbers and tooling — do NOT hardcode outdated versions.\n\n"
            f"{snippets}"
        )
    except Exception as exc:
        logger.warning("framework research: web search failed for %s: %s", tech_type, exc)
        return None


def _node_check_command(pkg_dir: Path) -> str | None:
    """Return the best build-check command for a Node.js project directory."""
    import json as _json
    try:
        data = _json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
    except Exception:
        data = {}
    scripts = data.get("scripts", {})
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}

    pm = "pnpm" if (pkg_dir / "pnpm-lock.yaml").exists() else \
         "yarn" if (pkg_dir / "yarn.lock").exists() else "npm"

    # Prefer fast type/lint checks over full bundle builds
    for script in ("typecheck", "type-check", "check", "lint", "build"):
        if script in scripts:
            return f"{pm} run {script}"
    # TypeScript project with no explicit script
    if "typescript" in deps or "@types/node" in deps:
        return "npx tsc --noEmit"
    return None


def _python_check_command(py_dir: Path) -> str:
    """Return a syntax-check command for a Python project directory."""
    # compileall is a stdlib module — always available, no imports executed,
    # recursively checks every .py file for syntax errors.
    exclude = r"(\.venv|venv|__pycache__|node_modules|\.git|dist|build)"
    return f'python -m compileall -q -x "{exclude}" .'


def _detect_build_commands(root: Path) -> list[tuple[Path, str]]:
    """
    Walk root and immediate subdirectories looking for build systems.
    Returns [(directory, command)] for every system found.
    Handles monorepos with separate frontend/backend directories.
    """
    _SKIP = {"node_modules", ".git", "__pycache__", ".venv", "venv",
             "dist", "build", ".next", ".nuxt", "coverage", "target", "bin", "obj"}

    checks: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def _probe(d: Path) -> None:
        if d in seen or not d.is_dir():
            return
        seen.add(d)

        # Node.js
        if (d / "package.json").exists():
            cmd = _node_check_command(d)
            if cmd:
                checks.append((d, cmd))
            return  # don't recurse into node sub-packages

        # Python
        if (d / "pyproject.toml").exists() or (d / "requirements.txt").exists() or (d / "setup.py").exists():
            checks.append((d, _python_check_command(d)))
            return

        # .NET
        csproj = next(d.glob("*.csproj"), None) or next(d.glob("*.sln"), None) or next(d.glob("*.fsproj"), None)
        if csproj:
            checks.append((d, "dotnet build --nologo -v q"))
            return

        # Go
        if (d / "go.mod").exists():
            checks.append((d, "go build ./..."))
            return

        # Rust
        if (d / "Cargo.toml").exists():
            checks.append((d, "cargo check"))
            return

        # Java — Maven
        if (d / "pom.xml").exists():
            checks.append((d, "mvn compile -q"))
            return

        # Java — Gradle
        if (d / "build.gradle").exists() or (d / "build.gradle.kts").exists():
            gradle = "./gradlew" if (d / "gradlew").exists() else "gradle"
            checks.append((d, f"{gradle} compileJava -q"))
            return

    # Probe root first, then immediate subdirectories (monorepo layout)
    _probe(root)
    if not checks:
        for sub in sorted(root.iterdir()):
            if sub.is_dir() and sub.name not in _SKIP:
                _probe(sub)

    return checks


async def _run_build(output_dir: str, command: str | None = None) -> dict:
    """
    Auto-detect the build system(s) in output_dir and run the appropriate
    check command. Handles Node/Python/.NET/Go/Rust/Java and monorepos
    with separate frontend+backend directories.
    """
    root = Path(output_dir)

    if command:
        # Explicit override — run exactly what was requested
        result = await run_shell_command(command, output_dir, timeout_seconds=180)
        combined = (result.stdout + "\n" + result.stderr).strip()
        if len(combined) > 5000:
            combined = "…(truncated)\n" + combined[-5000:]
        return {
            "success": result.exit_code == 0,
            "exit_code": result.exit_code,
            "command": command,
            "output": combined,
            "timed_out": result.timed_out,
        }

    checks = _detect_build_commands(root)
    if not checks:
        return {
            "success": None,
            "output": (
                "No recognized build system found. Looked for: package.json, pyproject.toml, "
                "requirements.txt, *.csproj, *.sln, go.mod, Cargo.toml, pom.xml, build.gradle"
            ),
        }

    all_output: list[str] = []
    overall_success = True

    for check_dir, cmd in checks:
        try:
            rel = check_dir.relative_to(root).as_posix()
        except ValueError:
            rel = str(check_dir)
        label = rel if rel != "." else "project root"

        result = await run_shell_command(cmd, str(check_dir), timeout_seconds=180)
        combined = (result.stdout + "\n" + result.stderr).strip()
        if len(combined) > 3000:
            combined = "…(truncated)\n" + combined[-3000:]
        status = "✓ passed" if result.exit_code == 0 else f"✗ exit {result.exit_code}"
        all_output.append(f"[{label}] $ {cmd}  →  {status}\n{combined}".rstrip())
        if result.exit_code != 0:
            overall_success = False

    full_output = "\n\n".join(all_output)
    if len(full_output) > 6000:
        full_output = "…(truncated — showing last 6000 chars)\n" + full_output[-6000:]

    return {
        "success": overall_success,
        "checks": [{"dir": str(d.relative_to(root)), "command": c} for d, c in checks],
        "output": full_output,
    }
def _write_progress_md(
    output_dir: str,
    prd_sections: list[dict],
    completed_tasks: list[dict],
    idea_name: str,
    round_idx: int,
) -> None:
    """Write docs/PROGRESS.md — a compact live-state file agents can read instead of re-scanning everything."""
    try:
        from datetime import UTC, datetime as _dt
        now = _dt.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        impl_tasks = [t for t in completed_tasks if not t.get("id", "").startswith("_")]
        all_written: list[str] = []
        for t in impl_tasks:
            all_written.extend(t.get("files_written", []))
        unique_files = sorted(set(all_written))

        lines: list[str] = [
            f"# PROGRESS: {idea_name}",
            "",
            f"Updated: {now} | Round {round_idx + 1} | "
            f"{len(impl_tasks)} task(s) complete | {len(unique_files)} file(s) written",
            "",
        ]

        if prd_sections:
            lines += ["## PRD Sections (for reference)", ""]
            for s in prd_sections:
                heading = s["heading"] if isinstance(s, dict) else str(s)
                lines.append(f"- {heading}")
            lines.append("")

        lines += ["## Completed Tasks", ""]
        if impl_tasks:
            for t in impl_tasks:
                icon = "✓" if t.get("success") else "✗"
                fw = t.get("files_written", [])
                if fw:
                    sample = ", ".join(fw[:3])
                    extra = f"…+{len(fw) - 3}" if len(fw) > 3 else ""
                    files_note = f" ({len(fw)} files: {sample}{extra})"
                else:
                    files_note = ""
                lines.append(f"- {icon} **{t['title']}**{files_note}")
                if t.get("blocker"):
                    lines.append(f"  - Blocker: {t['blocker']}")
        else:
            lines.append("_No tasks completed yet._")

        if unique_files:
            lines += ["", "## All Files Written", ""]
            for f in unique_files[:80]:
                lines.append(f"- {f}")
            if len(unique_files) > 80:
                lines.append(f"… ({len(unique_files) - 80} more)")

        out_path = Path(output_dir) / "docs" / "PROGRESS.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info(
            "progress_md: wrote PROGRESS.md (round %d, %d tasks, %d files)",
            round_idx + 1, len(impl_tasks), len(unique_files),
        )
    except Exception as exc:
        logger.warning("progress_md: failed to write: %s", exc)


def _read_services_json(output_dir: str) -> dict | None:
    """Read SERVICES.json from the output directory, returning None if absent or malformed."""
    try:
        path = Path(output_dir) / "SERVICES.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("services_json: failed to read: %s", exc)
    return None


_MAX_PRD_CHARS = 24_000


def _runtime_context() -> str:
    """Return a short environment block injected at the top of every agent system prompt."""
    now = datetime.now()
    system = platform.system()          # "Windows" | "Linux" | "Darwin"
    shell = "PowerShell" if system == "Windows" else "bash"
    shell_note = (
        "Use PowerShell syntax for shell commands: `dir` (not `ls`), `type` (not `cat`), "
        "backslash path separators, `$env:VAR` for environment variables. "
        "Do NOT use bash-only syntax (`&&`, `||` pipeline chaining, `export VAR=x`, backtick substitution)."
        if system == "Windows"
        else "Use bash syntax for shell commands."
    )
    return (
        f"## Runtime environment\n\n"
        f"Date/time: {now.strftime('%Y-%m-%d %H:%M')} (local)\n"
        f"OS: {system}\n"
        f"Shell: {shell}\n"
        f"{shell_note}\n\n"
    )


def _orchestrator_system_prompt(prd_content: str, framework_research: str | None = None) -> str:
    prd_excerpt = prd_content[:_MAX_PRD_CHARS]
    if len(prd_content) > _MAX_PRD_CHARS:
        prd_excerpt += "\n... (PRD truncated for context)"

    task_schema = (
        '{"id": "snake_case_id", '
        '"plan_task_id": "EXACT id from plan_next() — required when dispatching a plan task", '
        '"title": "short title ≤ 60 chars", '
        '"task_type": "implement", '
        '"instruction": "complete self-contained instructions — list all files to create, '
        'their purpose, cross-file dependencies, and commands to run"}'
    )

    return (
        _runtime_context()
        + "You are an orchestrator agent for a software implementation pipeline.\n\n"
        "## Your role\n\n"
        "You read the PRD, check what has already been built, and delegate one concrete task at a time "
        "to a sub-agent. YOU do NOT write files or run commands — you only read the project state to "
        "understand what is done and what remains.\n\n"
        "## Available tools\n\n"
        "You may call `list_files`, `inspect_files`, `grep_files`, `memory_search`, and `memory_list` "
        "to inspect the project.\n"
        "- `list_files`: list directory contents to understand project layout\n"
        "- `inspect_files`: read up to 10 files at once and return their content (truncated) plus "
        "stub-detection markers. Use this to inspect multiple files in ONE round — far more efficient "
        "than individual file reads. Pass up to 10 file paths at a time.\n"
        "- `grep_files`: search across files for a pattern\n"
        "- `memory_list()`: list all files that sub-agents have already read and analysed. **Call this "
        "at the start of every round** — it tells you which files have stored observations so you do "
        "not dispatch redundant inspection tasks.\n"
        "- `memory_search(query)`: semantic search over stored file observations. Call this BEFORE "
        "dispatching a task for feature X to check whether any agent has already implemented it. "
        "Returns file paths and brief observations. Use it to avoid duplicate implementations.\n"
        "Do NOT call `file_edit`, `read_file`, or `run_shell` — those are reserved for sub-agents.\n"
        "If you need a shell command run (e.g. `poetry install`, `npm install`, `dotnet restore`), "
        "dispatch a task with that command in the instruction — sub-agents have `run_shell`. "
        "NEVER set `user_message` because a tool is unavailable; dispatch a task instead.\n\n"
        "## Plan management — your persistent task list\n\n"
        "Use the plan tools to track every task you intend to implement. "
        "The plan is stored on disk and survives restarts.\n\n"
        "**Workflow:**\n"
        "1. **Round 1**: call `plan_list` — if empty, build the full plan now by calling `plan_add` "
        "for every task derived from the PRD. Scope each to exactly 1 file before adding.\n"
        "2. **Each round**: call `plan_next` to get the next task. "
        "Call `plan_update(id, status='in_progress')` before dispatching it. "
        "Use its `instruction` field as the task instruction in `next_tasks`. "
        "**CRITICAL**: copy the EXACT `id` from the plan task into the `next_tasks` entry — "
        "the runtime matches dispatched tasks to plan tasks by id. "
        "If the ids don't match, the plan never advances and the orchestrator loops forever.\n"
        "   - If `plan_next` returns `retry=true`, the task previously failed. "
        "Read its `notes` field and use a different approach in the instruction.\n"
        "   - If `plan_next` returns a `notice` about later-done tasks, the task may be obsolete — "
        "call `plan_update(id, status='done')` or `plan_remove(id)` before dispatching.\n"
        "3. **After success**: call `plan_update(id, status='done')`.\n"
        "4. **After permanent failure** (⛔ REFRAME REQUIRED in history): call `plan_remove(id)`, "
        "then `plan_add` 2–3 smaller replacement tasks each touching exactly 1 file.\n"
        "   - Use `plan_update(id, status='skipped')` when a task is no longer needed due to a "
        "plan change or because another task already covered it. Skipped tasks are ignored by "
        "plan_next — this lets you change direction without getting stuck.\n"
        "5. **When plan_next returns null**: all tasks complete — set done=true.\n\n"
        "- `plan_list(offset, limit)` — paginated view of the plan (default limit=20)\n"
        "- `plan_add(id, title, instruction)` — add a pending task\n"
        "- `plan_update(id, status?, title?, instruction?, notes?)` — update a task\n"
        "- `plan_remove(id)` — remove a task (use before adding replacement tasks)\n"
        "- `plan_next()` — get the next in_progress or pending task\n\n"
        "## Deduplication — search memory before assigning\n\n"
        "Before dispatching a task that creates a new utility or module, call `memory_search('X')` "
        "where X is the feature or concept (e.g. 'authentication', 'API client', 'database connection'). "
        "If memory returns an observation for an existing file that covers it, the new task must IMPORT "
        "from that file — never create a duplicate implementation.\n"
        "Common duplication traps to prevent: auth helpers in both `utils/auth.py` and `services/auth.py`; "
        "API clients in `api/client.ts` and `utils/api.ts`; config loaders in multiple files. "
        "When your task instruction references an existing module, quote the file path explicitly: "
        "'import from `src/utils/auth.py` — do NOT recreate its functions in this file.'\n\n"
        "## Product Requirements Document\n\n"
        f"{prd_excerpt}\n\n"
        "## Output format\n\n"
        "After any tool use (or immediately), output a JSON object only — no prose:\n"
        '{"analysis": "what has been done and what remains", '
        f'"next_tasks": [{task_schema}], '
        '"done": false, "user_message": null}\n\n'
        "You may include 1–2 tasks in `next_tasks`. **Prefer 1 task per batch** — only use 2 when "
        "both tasks are truly independent and each is immediately ready to implement. "
        "Each task must be atomic: implement exactly one logical unit (one module, one component, "
        "one endpoint, one service). **Each task must touch exactly 1 file.** "
        "A large feature must be split across multiple sequential batches, not crammed into one task. "
        "Tasks in the same batch MUST target completely independent files — no two tasks in a batch "
        "may write to the same file.\n\n"
        "**Task scope limit — hard rule**: a task that names more than 1 file OR more than 3 functions "
        "is too large and WILL fail. Split it. "
        "BAD: 'Implement player solution submission and voting system' (two systems, many files). "
        "GOOD: 'Implement solution submission endpoint' (one route, one file), then separately "
        "'Implement vote tallying logic' (one module, one file).\n\n"
        "When all PRD sections are implemented:\n"
        '{"analysis": "all tasks complete", "next_tasks": [], "done": true, "user_message": null}\n\n'
        "When you need the user to make a decision before you can proceed:\n"
        '{"analysis": "...", "next_tasks": [], "done": false, '
        '"user_message": "the specific question or decision you need from the user"}\n\n'
        "## Build-first development methodology — mandatory ordering\n\n"
        "Think like an experienced developer: first make something that runs, then add features one by one.\n\n"
        "**Milestone 0 — Runnable shell (ALWAYS the first task batch, no exceptions)**\n"
        "Your very first task must produce a minimal project that builds and can be started:\n"
        "- **Declare the folder layout first** — state in the task instruction whether source files go under "
        "`src/` or at the project root. The sub-agent must pick ONE and use it for every source file. "
        "Record this as `Source layout: src/` or `Source layout: project root` in `TECH_DECISIONS.md`.\n"
        "- Entry point (main.py, src/index.ts, App.svelte, etc.) — bare minimum, just enough to start\n"
        "- Build/dependency config (package.json / pyproject.toml / Cargo.toml) with top-level deps listed\n"
        "- Required config files (vite.config.ts, tsconfig.json, .env.example, etc.)\n"
        "- Run install (npm install / pip install -r requirements.txt) in the same task\n"
        "After this task: call `run_build`. If it fails, dispatch ONE fix task and call `run_build` again. "
        "Repeat until the build is clean. Do NOT move to Milestone 1 until the shell builds.\n\n"
        "**Milestone 1+ — One feature at a time, end to end**\n"
        "Once the shell builds, implement features from the PRD one at a time:\n"
        "- Pick ONE feature — not a layer ('all routes', 'all models'), but one complete user-facing capability\n"
        "- Implement it in sequential tasks: data model → business logic → API/route → UI (if applicable)\n"
        "- After the feature's last task: call `run_build`\n"
        "- If it fails: fix the build before touching the next feature\n"
        "- Only after a passing build may you begin the next feature\n\n"
        "**Build gate — hard rule**: A failing build blocks ALL feature work. "
        "Never dispatch a feature task when the last `run_build` result was non-zero. "
        "If you have not yet called `run_build` this session, do it now before dispatching anything.\n\n"
        "**Logic before UI within each feature**: for features that have both logic and UI, implement "
        "the logic (pure functions, data models) before the UI (components, rendering). "
        "Never mix logic and UI in the same file — that is a task split.\n\n"
        "## Integration wiring — mandatory checkpoints\n\n"
        "After every 3 implementation batches (not counting scaffold), dispatch one 'Integration Wiring' task:\n"
        "1. Read the app entry point and every file it imports directly\n"
        "2. For every `import { X } from './module'` — verify X is actually exported from that module\n"
        "3. For React: verify every `<Component prop={val} />` matches the component's actual prop names\n"
        "4. For function calls: verify argument count and order match the called signature\n"
        "This task WRITES real fixes — it is not exploration. Dispatch it as a real sub-agent task.\n"
        "Common failures: hook file exports plain functions instead of a React hook; parent passes "
        "`config={{...}}` when child expects separate props; service called with wrong argument count.\n\n"
        "## Build verification — use run_build\n\n"
        "You have a `run_build` tool that runs the project build command and returns compiler errors. "
        "Call it: (a) after every scaffold task, (b) after completing each feature, "
        "(c) whenever the build status is unknown. Build errors tell you exactly which file and line "
        "is broken — every error is a concrete task to add.\n\n"
        "**When build fails — act immediately, do not analyse**: Read the first error: file name, line "
        "number, error message. Dispatch ONE task that says 'Fix [file]:[line] — [error]'. "
        "Do NOT spend tokens reasoning about escape characters, backslash syntax, string quoting, "
        "or encoding. The sub-agent will fix the actual source; you just need to name the file and error.\n\n"
        "## task_type field\n\n"
        "Every task must include one of:\n"
        "- `\"task_type\": \"implement\"` (default) — task creates or modifies source files\n"
        "- `\"task_type\": \"inspect\"` — pure read/assess task, no file writes\n"
        "- `\"task_type\": \"scaffold\"` — task sets up the initial project structure so the build passes. "
        "May include CLI commands (`dotnet new sln`, `npm init`, `cargo new`, `vite create`) AND "
        "writing minimal entry-point or config files (main.py, index.ts, App.svelte, etc.). "
        "The sub-agent will NOT read the PRD for feature requirements and will NOT implement any "
        "business logic — only the skeleton needed to get a clean build. "
        "**Use this for Milestone 0 and any task whose sole goal is project structure / build health, "
        "not feature implementation.**\n"
        "Do NOT dispatch inspect tasks when you can use `inspect_files` or `grep_files` directly.\n\n"
        "## Task instruction guidelines\n\n"
        "The sub-agent receives only the `instruction` field, the output directory path, and the full PRD. Make it:\n"
        "- PRD-grounded: quote the exact rules, conditions, or state definitions from the PRD's "
        "`Logic Specification` section that this task implements. Do not paraphrase — copy the "
        "if/when→then rules verbatim so the sub-agent cannot substitute a simpler version.\n"
        "- Logic-first for logic tasks: instruct the sub-agent to implement logic as pure functions "
        "with no UI imports. State must be plain data structures. No DOM, no event listeners.\n"
        "- Fully self-contained: list every file by path with its purpose\n"
        "- Tech-stack specific: name libraries, exact versions, and patterns to use\n"
        "- Context-aware: tell the sub-agent which existing files to read for context first\n"
        "- Structure-consistent: before writing file paths in an instruction, check completed task results "
        "to detect the established source root (e.g. `src/`). Every path in your instruction MUST use "
        "that same root. If earlier tasks used `src/components/Foo.tsx`, new component paths must also "
        "start with `src/` — never `components/Foo.tsx` at the project root.\n"
        "- Command-aware: list any post-write commands (build, test) to run; if this task should run `npm install` "
        "or equivalent, say so explicitly — sub-agents will NOT install packages unless told to\n"
        f"- Shell environment: {shell_environment_context()}\n\n"
        "## Technology lock — decide once, enforce always\n\n"
        "In your FIRST or SECOND task batch, include a task to write `TECH_DECISIONS.md` at the project root. "
        "It must record: chosen backend framework (e.g. FastAPI, Flask, Express), frontend framework "
        "(e.g. React, Svelte, Vue), database, any other stack-level choices, AND the source layout — "
        "write exactly `Source layout: src/` or `Source layout: project root` so the build system can "
        "read it and enforce the layout in every subsequent task. "
        "Every subsequent task instruction MUST state the chosen stack explicitly — "
        "sub-agents must not make independent technology choices. "
        "Never dispatch two tasks that could independently pick conflicting frameworks "
        "(e.g. one task creates a Flask app while another creates a FastAPI app). "
        "If you detect conflicting files (two different framework entry points), dispatch a consolidation task "
        "that deletes the wrong one before any further implementation.\n\n"
        "## Framework-specific scaffold rules\n\n"
        "The scaffold task MUST use the correct CLI tooling to create project structure. "
        "Never hand-write build config files that the framework's CLI is supposed to generate.\n\n"
        "### .NET (any project with .csproj / .sln)\n"
        "Use `\"task_type\": \"scaffold\"` for solution/project creation tasks.\n"
        "NEVER write .csproj or .sln files by hand — always create them with the dotnet CLI:\n"
        "1. `dotnet new sln -n <SolutionName>` — creates the .sln file at the project root\n"
        "2. For each project: `dotnet new <template> -n <ProjectName> --framework net9.0`\n"
        "   Templates: `classlib`, `console`, `wpf`, `winforms`, `web`, `webapi`, `maui`, `avalonia.app`\n"
        "3. `dotnet sln add <ProjectName>/<ProjectName>.csproj` — register each project in the solution\n"
        "4. `dotnet add <ProjectName> reference <DepProject>/<DepProject>.csproj` — project-to-project refs\n"
        "5. `dotnet add <ProjectName> package <NuGetPackage>` — NuGet dependencies\n"
        "6. `dotnet restore` — restore all packages\n"
        "7. `dotnet build <SolutionName>.sln` — verify the solution builds\n"
        "The scaffold task must run these commands. Do NOT write any .cs files until the project "
        "structure exists on disk. If a task writes .cs files into a directory that has no .csproj, "
        "those files will be silently ignored by the build.\n\n"
        "### Node.js\n"
        "After writing package.json, run `npm install` (or yarn/pnpm install) in the same task.\n\n"
        "### Python\n"
        "After writing requirements.txt or pyproject.toml, run `pip install -r requirements.txt` "
        "or `pip install -e .` in the same task.\n\n"
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
        + (f"{framework_research}\n\n" if framework_research else "")
        + "## Startup scripts — mandatory before done=true\n\n"
        "Sub-agents register every runnable service in `SERVICES.json` at the project root. "
        "When you are about to set `done: true`, check the current user prompt for a SERVICES.json block. "
        "If it is present and a startup script has NOT yet been completed, you MUST dispatch a "
        "'Create startup scripts' task BEFORE setting `done: true`:\n"
        "  - Read SERVICES.json\n"
        "  - Write `start.sh` (Linux/Mac) and `start.bat` (Windows) that install dependencies "
        "and start each service in the correct order\n"
        "  - Update README.md with 'How to run' instructions including URLs/ports for each service\n"
        "Only after that task completes may you set `done: true`.\n\n"
        "## Critical rule — what sub-agent tasks ARE and ARE NOT\n\n"
        "Sub-agent tasks exist for ONE purpose only: **writing files and running commands**.\n"
        "They cannot help you explore the project — exploration is YOUR job via your own tools.\n\n"
        "FORBIDDEN task titles/purposes (these will be rejected automatically):\n"
        "- 'List files', 'List all files', 'Explore project', 'Read existing files', 'Inspect structure'\n"
        "- Any task whose sole purpose is to read, list, grep, or inspect — no file writing, no commands\n\n"
        "If you need to see the project state, call `list_files` or `inspect_files` RIGHT NOW in this response "
        "before deciding tasks. Do NOT create a sub-agent to do it for you.\n\n"
        "## Rules\n\n"
        "- Delegate 1–2 cohesive, independent tasks per response; prefer 1 task when unsure\n"
        "- **Exploration budget: maximum 2 rounds** (one `list_files` + one `inspect_files` batch). "
        "Dispatch tasks by round 3 at the latest — do not keep reading files. "
        "For bug-fix or startup requests, you do NOT need to identify the root cause yourself: "
        "write a task instruction that tells the sub-agent to investigate and fix, "
        "naming the specific files most likely to be relevant.\n"
        "- Tasks in the same batch must NOT overlap: each should own its own set of files\n"
        "- **Package manager installs** (`npm install`, `pip install -r`, etc.) must appear in at most ONE task per batch. "
        "If a batch has multiple tasks and one of them writes package.json / requirements.txt, assign the install "
        "to that task only. All other tasks in the batch must NOT include install commands in their instructions. "
        "If install is not needed yet, omit it entirely — do it in a later single-task batch.\n"
        "- **Never assign `npm run dev`, `npm start`, or any long-running dev server** to any task. "
        "Sub-agents must not start servers.\n"
        "- Track which PRD sections have been implemented and ensure full coverage\n"
        "- Set `done=true` only when all PRD sections are covered by completed tasks\n"
        "- Output ONLY the JSON object — no markdown fences, no extra text\n"
    )


def _read_declared_source_root(output_dir: str) -> str | None:
    """Read the source layout explicitly declared in TECH_DECISIONS.md, if present."""
    try:
        td = Path(output_dir) / "TECH_DECISIONS.md"
        if td.exists():
            text = td.read_text(encoding="utf-8")
            m = re.search(r'source\s+(?:layout|root)\s*:\s*`?([\w/]+)`?', text, re.IGNORECASE)
            if m:
                val = m.group(1).strip().rstrip("/").lower()
                # "root" / "project root" / "." mean no src sub-directory
                return val if val not in ("root", "project", "projectroot", ".", "") else None
    except Exception:
        pass
    return None


def _detect_project_structure(completed_tasks: list[dict], output_dir: str | None = None) -> dict:
    """
    Detect established folder layout. Priority:
    1. Declared in TECH_DECISIONS.md (authoritative — written by the scaffold task)
    2. Inferred from files_written in completed tasks (heuristic fallback)
    Returns {'source_root': 'src' | None, 'top_dirs': [...sorted top-level dirs...]}.
    """
    all_written: list[str] = []
    for t in completed_tasks:
        all_written.extend(t.get("files_written", []))

    src_count = sum(1 for p in all_written if p.startswith("src/"))

    top_dirs: set[str] = set()
    for p in all_written:
        parts = p.replace("\\", "/").split("/")
        if len(parts) > 1:
            top_dirs.add(parts[0])

    declared = _read_declared_source_root(output_dir) if output_dir else None
    return {
        "source_root": declared if declared is not None else ("src" if src_count >= 2 else None),
        "top_dirs": sorted(top_dirs),
    }


def _detect_build_state(completed_tasks: list[dict], output_dir: str | None = None) -> dict:
    """
    Derive build health from completed tasks.
    Returns: {has_scaffold, build_passed, build_failed, impl_batches}.
    """
    all_files: list[str] = []
    for t in completed_tasks:
        all_files.extend(t.get("files_written") or [])

    # Scaffold: detect from files_written (before output_dir exists on disk)
    _CONFIG = {"package.json", "pyproject.toml", "Cargo.toml", "requirements.txt",
               "go.mod", "pom.xml", "build.gradle", "build.gradle.kts"}
    has_scaffold = any(Path(f).name in _CONFIG or f.endswith(".csproj") or f.endswith(".sln")
                       for f in all_files)

    # Also check disk if output_dir provided — more reliable once files are written
    detected_systems: list[str] = []
    if output_dir and Path(output_dir).exists() and completed_tasks:
        for d, cmd in _detect_build_commands(Path(output_dir)):
            try:
                label = d.relative_to(Path(output_dir)).as_posix() or "."
            except ValueError:
                label = str(d)
            detected_systems.append(f"{label}: {cmd}")
        if detected_systems:
            has_scaffold = True

    # Build check history
    build_tasks = [t for t in completed_tasks if t.get("id", "").startswith("_build_check_")]
    last_build_passed = False
    last_build_failed = False
    if build_tasks:
        last_build_passed = bool(build_tasks[-1].get("success"))
        last_build_failed = not last_build_passed

    # Count real implementation batches (not scaffold, not build checks, not user inputs)
    impl_batches = sum(
        1 for t in completed_tasks
        if not t.get("id", "").startswith("_")
        and not t.get("id", "").startswith("user_input_")
    )

    return {
        "has_scaffold": has_scaffold,
        "build_passed": last_build_passed,
        "build_failed": last_build_failed,
        "build_checked": bool(build_tasks),
        "impl_batches": impl_batches,
        "detected_systems": detected_systems,
    }


def _extract_prd_sections(prd_content: str) -> list[dict]:
    """
    Return each ## / ### section with its heading and the bullet-point details beneath it.
    Returns list of {"heading": str, "details": str}.
    """
    sections: list[dict] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in prd_content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## ") or stripped.startswith("### "):
            if current_heading:
                sections.append({"heading": current_heading, "details": "\n".join(current_lines).strip()})
            current_heading = stripped.lstrip("#").strip()
            current_lines = []
        elif current_heading and stripped:
            current_lines.append(stripped)

    if current_heading:
        sections.append({"heading": current_heading, "details": "\n".join(current_lines).strip()})

    return sections


def _parse_orchestrator_result(orch_result: dict) -> tuple[str | None, bool, list[dict]]:
    """
    Parse the orchestrator JSON payload and resolve contradictory states.

    If the model returns both done=true and actionable next_tasks, treat the
    response as not done yet and execute the tasks first. This prevents the
    orchestration loop from jumping into verification prematurely.
    """
    user_message = orch_result.get("user_message")
    done = bool(orch_result.get("done", False))

    # Accept next_tasks (canonical), tasks (model non-compliance alias), and next_task (legacy single)
    raw_tasks = orch_result.get("next_tasks") or orch_result.get("tasks")
    if not raw_tasks and orch_result.get("next_task"):
        raw_tasks = [orch_result["next_task"]]
    next_tasks: list[dict] = [t for t in (raw_tasks or []) if isinstance(t, dict)]

    if done and next_tasks:
        logger.warning(
            "orchestrator: received contradictory response (done=true with %d task(s)); dispatching tasks before verification",
            len(next_tasks),
        )
        done = False

    return user_message, done, next_tasks


def _normalize_sub_agent_result(raw_result: object, task_title: str, task_type: str = "implement", actual_shell_calls: int = 0) -> dict:
    """
    Normalize a sub-agent result into the execution schema the orchestrator expects.

    Treat missing or wrong-shaped payloads as failures so the orchestrator does not
    silently accept empty planning-style responses as successful implementation work.
    For task_type="inspect", empty files_written/commands_run is valid and expected.
    """
    if not isinstance(raw_result, dict):
        return {
            "summary": f"Task '{task_title}' returned a non-JSON result.",
            "files_written": [],
            "commands_run": [],
            "success": False,
            "blocker": "Sub-agent returned a non-dict result",
        }

    # Normalize common key-name aliases before validating — small models often use
    # "message" instead of "summary", "files" instead of "files_written", etc.
    if "summary" not in raw_result and any(k in raw_result for k in ("message", "files", "commands")):
        raw_result = dict(raw_result)
        if "message" in raw_result:
            raw_result.setdefault("summary", raw_result.pop("message"))
        if "files" in raw_result and "files_written" not in raw_result:
            raw_result["files_written"] = raw_result.pop("files")
        if "commands" in raw_result and "commands_run" not in raw_result:
            raw_result["commands_run"] = raw_result.pop("commands")
    # After normalization, if summary is still absent it's genuinely a planning payload
    if "summary" not in raw_result and any(k in raw_result for k in ("message", "files", "commands")):
        return {
            "summary": f"Task '{task_title}' returned a planning payload instead of an execution result.",
            "files_written": [],
            "commands_run": [],
            "success": False,
            "blocker": "Sub-agent returned planning JSON instead of execution summary",
        }

    summary = str(raw_result.get("summary") or "").strip()
    files_written = [str(p) for p in (raw_result.get("files_written") or []) if isinstance(p, str) and p.strip()]
    commands_run = [str(c) for c in (raw_result.get("commands_run") or []) if isinstance(c, str) and c.strip()]
    blocker_raw = raw_result.get("blocker")
    blocker = str(blocker_raw).strip() if blocker_raw not in (None, "") else None
    success_raw = raw_result.get("success")

    # Detect fabricated capability complaints in the blocker.
    # Small models hallucinate "execution restrictions" or "cannot verify" even
    # though run_shell and run_shell_background are available. If the agent
    # actually wrote files, strip the fake blocker and treat it as success.
    _FAKE_BLOCKER_PHRASES = (
        "execution restriction",
        "cannot verify",
        "unable to verify",
        "not able to verify",
        "cannot execute",
        "unable to execute",
        "execution environment",
        "not available in this environment",
        "cannot run",
        "unable to run",
        "tool is not available",
        "don't have access",
        "do not have access",
        "accessibility",
        "cannot access",
    )
    if blocker and any(p in blocker.lower() for p in _FAKE_BLOCKER_PHRASES):
        if files_written:
            logger.warning(
                "normalize_sub_agent: stripping fabricated capability blocker (files were written): %r",
                blocker[:120],
            )
            blocker = None
            success_raw = True
        else:
            logger.warning(
                "normalize_sub_agent: capability complaint blocker but no files written: %r",
                blocker[:120],
            )

    # Coerce common non-bool representations before the isinstance check.
    # Small models frequently output "success": "true" (string) or "success": 1 (int).
    if not isinstance(success_raw, bool) and success_raw is not None:
        if str(success_raw).lower() in ("true", "yes", "1"):
            success_raw = True
        elif str(success_raw).lower() in ("false", "no", "0"):
            success_raw = False

    if isinstance(success_raw, bool):
        success = success_raw
    elif files_written or commands_run:
        # Model omitted the success field but did produce observable work — infer success.
        logger.warning(
            "normalize_sub_agent: missing 'success' field but %d file(s)/%d command(s) present — inferring success",
            len(files_written), len(commands_run),
        )
        success = True
    else:
        success = False
        if not blocker:
            blocker = "Sub-agent result missing required boolean 'success' field"

    if success and blocker:
        success = False

    if success and not summary:
        summary = f"Completed task '{task_title}'."

    if success and not files_written and not commands_run and task_type != "inspect" and actual_shell_calls == 0:
        success = False
        blocker = blocker or "Sub-agent reported success but produced no files or commands"
        if not summary:
            summary = f"Task '{task_title}' produced no observable work."

    if not success and not summary:
        summary = f"Task '{task_title}' failed."

    if not success and not blocker:
        blocker = "Sub-agent returned an unsuccessful result without a blocker"

    return {
        "summary": summary,
        "files_written": files_written,
        "commands_run": commands_run,
        "success": success,
        "blocker": blocker,
    }


def _expand_inspection_paths(output_dir: str, paths: list[str], max_files: int = 10) -> list[str]:
    """
    Convert inspect_files inputs into concrete file paths.

    The planner sometimes passes directories like `src` or `backend` even though
    the inspector is file-oriented. Expand those directories into real files so
    the inspector can summarize code directly instead of running another broad
    exploration loop.
    """
    if not output_dir:
        return [p.replace("\\", "/") for p in paths[:max_files]]

    base = Path(output_dir).resolve()
    skip_dirs = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".next"}
    out: list[str] = []
    seen: set[str] = set()

    def _add_file(path_obj: Path) -> None:
        rel = str(path_obj.relative_to(base)).replace("\\", "/")
        if rel not in seen:
            seen.add(rel)
            out.append(rel)

    for raw_path in paths:
        if len(out) >= max_files:
            break
        rel_path = (raw_path or "").strip()
        if not rel_path:
            continue
        try:
            target = (base / rel_path).resolve()
        except Exception:
            continue
        if not str(target).startswith(str(base)) or not target.exists():
            continue
        if target.is_file():
            _add_file(target)
            continue
        if not target.is_dir():
            continue

        for child in sorted(target.rglob("*")):
            if len(out) >= max_files:
                break
            if any(part in skip_dirs for part in child.parts):
                continue
            if not child.is_file():
                continue
            _add_file(child)

    return out


_PY_STUB_RE = re.compile(
    r"\n[ \t]*(?:async\s+)?def\s+\w+[^\n]*:\n"
    r"(?:[ \t]*(?:\"\"\"[^\n]+\"\"\"|'''[^\n]+'''|#[^\n]*)?\n)*"
    r"[ \t]+(?:pass|return\s*None|return\s*\{\}|return\s*\[\]|return\s*\(\)|\.\.\.)\s*(?:\n|$)",
    re.MULTILINE,
)
_TS_STUB_RE = re.compile(
    r"=\s*(?:async\s+)?\([^)]{0,120}\)\s*(?::\s*[\w<>\[\]|&,\s]+?)?\s*=>\s*\{\s*\}",
)


def _has_semantic_stubs(text: str, suffix: str) -> bool:
    """Detect hollow function bodies not caught by text-marker search."""
    if suffix == ".py":
        return bool(_PY_STUB_RE.search(text))
    if suffix in (".ts", ".tsx", ".js", ".jsx"):
        return bool(_TS_STUB_RE.search(text))
    return False


def _build_file_tree(output_dir: str, max_files: int = 120) -> str | None:
    """
    Return a flat sorted list of all source files in output_dir, skipping noise dirs.
    Injected into the orchestrator prompt so it never needs to call list_files.
    """
    root = Path(output_dir)
    if not root.exists():
        return None
    _skip = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".nuxt", "coverage"}
    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _skip for part in path.relative_to(root).parts):
            continue
        lines.append(path.relative_to(root).as_posix())
        if len(lines) >= max_files:
            lines.append("… (truncated)")
            break
    return "\n".join(lines) if lines else None


def _orchestrator_user_prompt(
    idea: Idea,
    branch: SolutionBranch,
    completed_tasks: list[dict],
    follow_up_message: str | None = None,
    pending_user_messages: list[str] | None = None,
    verify_prd: bool = False,
    prd_sections: list[str] | None = None,
    interface_summary: str | None = None,
    file_tree: str | None = None,
    services_json: dict | None = None,
    output_dir: str | None = None,
) -> str:
    history_block = ""
    if completed_tasks:
        lines = []
        # Verification rounds already have a large PRD-sections checklist; cap the history
        # so the combined prompt stays within the model's context window.
        show_tasks = completed_tasks
        if verify_prd and len(completed_tasks) > 15:
            omitted = len(completed_tasks) - 15
            lines.append(f"*(…{omitted} earlier tasks omitted — showing last 15)*")
            show_tasks = completed_tasks[-15:]
        for t in show_tasks:
            icon = "✓" if t.get("success") else "⚠"
            lines.append(f"{icon} **{t['title']}**: {t.get('summary', '')}")
            fw = t.get("files_written", [])
            if fw:
                sample = ", ".join(fw[:5])
                extra = f" (+{len(fw) - 5} more)" if len(fw) > 5 else ""
                lines.append(f"   Files: {sample}{extra}")
            if t.get("blocker"):
                lines.append(f"   ⚠ Blocker: {t['blocker']}")
            if t.get("permanently_failed"):
                lines.append(
                    "   ⛔ PERMANENTLY FAILED: all fallback models failed this task. "
                    "Do NOT re-dispatch the same scope. "
                    "Break it into 2–3 smaller replacement tasks, each touching exactly 1 file "
                    "and implementing exactly one function, class, or route. "
                    "If plan tools are available, remove this task from the plan first, then add the smaller ones."
                )
        history_block = "\n\n## Completed Tasks\n\n" + "\n".join(lines)

    build_state = _detect_build_state(completed_tasks, output_dir=output_dir)

    # Determine plan state so we can inject mandatory plan-bootstrap hints
    _plan_is_empty = True
    _plan_pending_count = 0
    if output_dir:
        _plan_path = Path(output_dir) / ".think-plan.json"
        if _plan_path.is_file():
            try:
                _plan_data = json.loads(_plan_path.read_text(encoding="utf-8"))
                _plan_tasks = _plan_data.get("tasks", [])
                _plan_is_empty = len(_plan_tasks) == 0
                _plan_pending_count = sum(
                    1 for t in _plan_tasks if t.get("status") in ("pending", "in_progress", "failed")
                )
            except Exception:
                pass

    # On resume the project already had a working build in the previous run.
    # Suppress the "not verified" banner so it doesn't fight with the resume context.
    _is_resume = any(t.get("id") == "_resume_context" for t in completed_tasks)

    # Build-state banner — injected on every round so the model cannot ignore it
    build_banner = ""
    if not follow_up_message:
        if build_state["build_failed"]:
            build_banner = (
                "\n\n## 🚨 BUILD IS FAILING — ALL FEATURE WORK IS BLOCKED\n\n"
                "The last `run_build` returned errors. You MUST fix the build before dispatching any "
                "new feature or content task. Dispatch ONE build-fix task now, then call `run_build` again."
            )
        elif build_state["has_scaffold"] and not build_state["build_checked"] and not _is_resume:
            systems_hint = ""
            if build_state.get("detected_systems"):
                systems_hint = " Detected: " + "; ".join(build_state["detected_systems"]) + "."
            build_banner = (
                "\n\n## ⚠ Build not verified yet\n\n"
                f"Files exist but `run_build` has not been called.{systems_hint} "
                "Call it NOW before dispatching the next feature — a broken build blocks all forward progress."
            )
        elif build_state["build_passed"] and build_state["impl_batches"] > 0 and build_state["impl_batches"] % 3 == 0:
            build_banner = (
                "\n\n## Build check due\n\n"
                "Call `run_build` to verify the project still compiles before adding more features."
            )

    if follow_up_message:
        start_hint = (
            f"## User follow-up request\n\n"
            f"{follow_up_message}\n\n"
            "Dispatch tasks that address this request. You have a 2-round exploration budget — "
            "call `list_files '.'` and `inspect_files` on the 3–5 files most likely relevant to the request, "
            "then dispatch immediately. Do NOT read the entire project. "
            "For bug-fix or startup issues, your task instruction should tell the sub-agent to "
            "investigate (name the suspect files) and fix — you do not need to diagnose the root cause yourself. "
            "Set `done=true` only when the request is fully implemented."
        )
    elif not completed_tasks:
        start_hint = (
            "## Project is empty — Milestone 0: build the runnable shell first\n\n"
            "Your ONLY goal right now is a minimal project that builds and can be started. "
            "Do NOT implement any PRD features yet — not data models, not routes, not UI components.\n\n"
            "DO NOT call `list_files` or `inspect_files` — there is nothing to explore yet.\n\n"
            "Dispatch ONE scaffold task that writes:\n"
            "- The entry point (main.py, src/index.ts, App.svelte, etc.) — bare minimum, just enough to start\n"
            "- The build/dependency config (package.json / pyproject.toml / Cargo.toml) with all top-level deps\n"
            "- Required config files (vite.config.ts, tsconfig.json, .gitignore, .env.example, etc.)\n"
            "- Runs the install command (npm install / pip install) in the same task\n"
            "After this task: call `run_build`. Fix any errors before moving to features."
        )
    elif not build_state["has_scaffold"]:
        start_hint = (
            "## Milestone 0 incomplete — scaffold must come first\n\n"
            "The project has no build config yet. Dispatch a scaffold task before any feature work. "
            "The scaffold must produce: entry point + build config + install command run."
        )
    else:
        if _plan_is_empty:
            _plan_hint = (
                "⚡ FIRST ACTION THIS ROUND: build your implementation plan.\n"
                "1. Call `plan_list()` — if it returns no tasks, call `plan_add()` for EVERY PRD "
                "feature/section before doing anything else.\n"
                "2. Each plan task must touch exactly 1 file and implement exactly ONE function, class, or route.\n"
                "3. Do NOT call `list_files`, `inspect_files`, or dispatch any task until the full "
                "plan is built.\n\n"
            )
        else:
            _plan_hint = (
                f"⚡ FIRST ACTION THIS ROUND: call `plan_next()` "
                f"({_plan_pending_count} task(s) still pending/in-progress).\n"
                "Then call `plan_update(id, status='in_progress')` before dispatching.\n"
                "After the sub-agent reports back, call `plan_update(id, status='done')` or `'failed'`.\n"
                "Do NOT call `inspect_files` or `list_files` before `plan_next()`.\n\n"
            )
        start_hint = (
            _plan_hint
            + "The full project file tree is shown above — do NOT call `list_files`. "
            "Call `inspect_files` directly on the 3–8 files most relevant to your next task, "
            "then dispatch immediately. One `inspect_files` call is your entire exploration budget.\n\n"
            "**If you find files with `has_stubs: true`, TODOs, truncated content, or placeholder code**, "
            "create a repair task for each affected file. Your task instruction MUST include:\n"
            "- The exact file path\n"
            "- The specific function, section, or line that is incomplete (quote it)\n"
            "- What the complete implementation should do (from the PRD)\n"
            "Vague instructions like 'fix incomplete files' are not acceptable — the sub-agent needs "
            "precise targets to fix the right thing."
        )

    feedback_block = ""
    if pending_user_messages:
        msgs = "\n".join(f"- {m}" for m in pending_user_messages)
        feedback_block = (
            f"\n\n## ⚠ USER INSTRUCTION — HIGHEST PRIORITY\n\n"
            f"{msgs}\n\n"
            "This instruction OVERRIDES your own assessment. "
            "Your next tasks MUST directly address what the user asked for. "
            "Do NOT continue with your own plan or fix other issues first — "
            "address the user's request in this batch, even if you think something else is more important. "
            "Only after the user's request is fulfilled may you resume other work."
        )

    structure_block = ""
    if completed_tasks:
        structure = _detect_project_structure(completed_tasks, output_dir)
        constraints: list[str] = []
        if structure["source_root"]:
            constraints.append(
                f"- Source root is `{structure['source_root']}/` — ALL new source files (components, "
                f"utilities, modules, hooks, styles, etc.) MUST go under `{structure['source_root']}/`. "
                f"Never place source files at the project root or in a different top-level directory."
            )
        if structure["top_dirs"]:
            dirs_str = ", ".join(f"`{d}/`" for d in structure["top_dirs"])
            constraints.append(
                f"- Established top-level directories: {dirs_str}. "
                f"New files should extend this layout, not contradict it."
            )
        if constraints:
            structure_block = (
                "\n\n## Detected project structure — ENFORCE IN ALL TASK INSTRUCTIONS\n\n"
                + "\n".join(constraints)
                + "\n\nEvery file path you write in a task instruction MUST be consistent with the above. "
                "If a file belongs under `src/`, say `src/components/Foo.tsx`, not `components/Foo.tsx`."
            )

    verify_block = ""
    if verify_prd:
        if prd_sections:
            section_checklist_lines = []
            for s in prd_sections:
                heading = s["heading"] if isinstance(s, dict) else s
                details = (s.get("details") or "") if isinstance(s, dict) else ""
                line = f"   ### {heading}"
                if details:
                    # Indent detail lines so they're clearly subordinate
                    indented = "\n".join(f"   > {dl}" for dl in details.splitlines()[:6])
                    line += f"\n{indented}"
                line += (
                    f"\n   → Default: **MISSING**. Call `inspect_files` on the file(s) that implement this. "
                    f"Upgrade to PARTIAL only if you see partial logic; upgrade to IMPLEMENTED only if you "
                    f"can quote a specific function or code block that fully satisfies this requirement."
                )
                section_checklist_lines.append(line)
            section_block = (
                "\n\nEach PRD section below starts as MISSING. You MUST call `inspect_files` "
                "on the relevant file(s) before you may classify a section as PARTIAL or IMPLEMENTED. "
                "Classification rules:\n"
                "- MISSING: no relevant file exists, or inspect_files returns has_stubs=true with no real logic\n"
                "- PARTIAL: some logic exists but key parts are stubbed, hardcoded, or incomplete\n"
                "- IMPLEMENTED: inspect_files shows complete, working code with no stubs — "
                "you must be able to cite the specific function or lines that prove it\n\n"
                "Do NOT classify based on filenames or assumptions. Read the actual code.\n\n"
                + "\n\n".join(section_checklist_lines)
            )
        else:
            section_block = (
                "\n\nVerify every PRD section is covered with working (non-stub) code. "
                "Each section starts as MISSING — call `inspect_files` on the relevant files "
                "and only upgrade to IMPLEMENTED after reading actual code that proves it."
            )
        verify_block = (
            "\n\n## PRD Verification Required\n\n"
            "You indicated the implementation is complete. Treat this as a skeptical audit — "
            "assume nothing is done until you have read the actual code that proves it.\n\n"
            "**EFFICIENCY RULE**: `inspect_files` returns full file content immediately — no LLM involved. "
            "Call it with up to 10 paths at once. Aim to read ALL project files in 2–3 `inspect_files` "
            "calls (one round each), then classify in the following round. "
            "Do NOT read one file per round — that is catastrophically slow.\n\n"
            "Complete ALL steps below in order. Skipping any step is not allowed.\n\n"
            "1. Call `list_files` to get the full current project file tree\n"
            "2. Call `inspect_files` with batches of up to 10 file paths. Group unrelated files "
            "together — the goal is to cover all implementation files in as few calls as possible. "
            "Any file with `has_stubs: true` is NOT implemented — stubs do not count.\n"
            f"3. {section_block.strip()}\n\n"
            "4. In your `analysis` field, for every PRD section write:\n"
            "   `SECTION NAME: STATUS — evidence: <quote the specific function name or code you saw>`\n"
            "   A classification with no quoted evidence is rejected.\n\n"
            "5. Check for structural problems:\n"
            "   - Only ONE build tool / package.json tree at the project root\n"
            "   - HTML entry points match the actual bundler output\n"
            "   - package.json scripts use paths relative to their own directory\n"
            "   - All packages are version-compatible\n"
            "6. Call `run_build` RIGHT NOW — do NOT spawn a sub-agent for this. "
            "The tool runs the build command and returns compiler errors immediately. "
            "If it exits non-zero, every error in the output is a concrete bug to fix — "
            "add a task for each class of error (e.g. 'Fix missing export X in module Y', "
            "'Fix prop name mismatch in Component Z') and set `done=false`.\n"
            "7. For every section classified PARTIAL or MISSING — add tasks to complete it "
            "and set `done=false`. Only sections with quoted evidence count as IMPLEMENTED.\n"
            "8. Only set `done=true` when ALL sections are IMPLEMENTED with evidence "
            "AND `run_build` exits 0."
        )

    interface_block = ""
    if interface_summary:
        interface_block = (
            f"\n\n{interface_summary}\n\n"
            "Use this to spot files with **⚠ NO EXPORTS** (likely stubs), prop name mismatches "
            "between callers and receivers, or imports that reference symbols not listed here. "
            "Assign wiring/repair tasks for any mismatch you see before continuing with new features."
        )

    tree_block = ""
    if file_tree and completed_tasks:
        tree_block = f"\n\n## Current project files\n\n```\n{file_tree}\n```"

    services_block = ""
    if services_json and services_json.get("services"):
        services_block = (
            "\n\n## SERVICES.json — registered entry points\n\n"
            "```json\n" + json.dumps(services_json, indent=2) + "\n```\n\n"
            "Use this when writing the startup script task instruction. "
            "If no 'Create startup scripts' task has been completed yet, dispatch it before setting done=true."
        )

    return (
        f"PROJECT: {idea.name}\n"
        f"DESCRIPTION: {idea.description}\n\n"
        f"SELECTED APPROACH: {branch.approach_summary or 'N/A'}\n"
        f"{history_block}"
        f"{build_banner}"
        f"{structure_block}"
        f"{tree_block}"
        f"{interface_block}"
        f"{services_block}"
        f"{feedback_block}\n\n"
        f"{start_hint}"
        f"{verify_block}"
    )



def _sub_agent_extra_tools() -> list:
    """Return the extra tools list for sub-agents. Media tools are included only when ComfyUI is configured."""
    from app.config import settings
    from app.inference.client import (
        READ_PRD_TOOL, GENERATE_IMAGE_TOOL,
        GENERATE_AUDIO_MUSIC_TOOL, GENERATE_AUDIO_SFX_TOOL, GENERATE_AUDIO_SPEECH_TOOL,
    )
    from app.memory import MEMORY_STORE_TOOL, MEMORY_SEARCH_TOOL, MEMORY_LIST_TOOL
    tools = [READ_PRD_TOOL, MEMORY_STORE_TOOL, MEMORY_SEARCH_TOOL, MEMORY_LIST_TOOL]
    if settings.comfyui_base_url:
        tools.extend([GENERATE_IMAGE_TOOL, GENERATE_AUDIO_MUSIC_TOOL, GENERATE_AUDIO_SFX_TOOL, GENERATE_AUDIO_SPEECH_TOOL])
    return tools


def _memory_handlers(project_id: str) -> dict:
    """Return custom tool handlers for the three memory tools, closed over project_id."""
    import app.memory as _mem

    async def _store(args: dict) -> dict:
        fp = (args.get("file_path") or "").strip()
        obs = (args.get("observation") or "").strip()
        if not fp or not obs:
            return {"error": "file_path and observation are required"}
        await _mem.store(project_id, fp, obs)
        return {"stored": True, "file_path": fp}

    async def _search(args: dict) -> dict:
        query = (args.get("query") or "").strip()
        top_k = min(int(args.get("top_k") or 5), 10)
        if not query:
            return {"error": "query is required"}
        results = await _mem.search(project_id, query, top_k)
        return {"results": results, "count": len(results)}

    async def _list(_args: dict) -> dict:
        entries = await _mem.list_all(project_id)
        return {"memories": entries, "count": len(entries)}

    return {
        "memory_store": _store,
        "memory_search": _search,
        "memory_list": _list,
    }


def _plan_handlers(output_dir: str) -> dict:
    """Return custom tool handlers for the five plan tools, backed by .think-plan.json."""
    import json as _json
    from pathlib import Path as _Path

    _plan_file = _Path(output_dir) / ".think-plan.json"

    def _load() -> dict:
        if _plan_file.exists():
            try:
                return _json.loads(_plan_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"tasks": []}

    def _save(data: dict) -> None:
        _plan_file.write_text(_json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    async def _plan_list(args: dict) -> dict:
        offset = max(0, int(args.get("offset") or 0))
        limit  = min(50, max(1, int(args.get("limit") or 20)))
        data   = _load()
        tasks  = data.get("tasks", [])
        page   = tasks[offset: offset + limit]
        counts = {s: sum(1 for t in tasks if t.get("status") == s)
                  for s in ("pending", "in_progress", "done", "failed")}
        return {
            "tasks": page,
            "total": len(tasks),
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < len(tasks),
            **counts,
        }

    async def _plan_add(args: dict) -> dict:
        task_id     = (args.get("id") or "").strip()
        title       = (args.get("title") or "").strip()
        instruction = (args.get("instruction") or "").strip()
        notes       = (args.get("notes") or "").strip() or None
        if not task_id or not title or not instruction:
            return {"error": "id, title, and instruction are required"}
        data  = _load()
        tasks = data.get("tasks", [])
        if any(t.get("id") == task_id for t in tasks):
            return {"error": f"task id '{task_id}' already exists"}
        tasks.append({"id": task_id, "title": title, "status": "pending",
                      "instruction": instruction, "notes": notes})
        data["tasks"] = tasks
        _save(data)
        return {"added": True, "id": task_id, "total": len(tasks)}

    async def _plan_update(args: dict) -> dict:
        task_id = (args.get("id") or "").strip()
        if not task_id:
            return {"error": "id is required"}
        data  = _load()
        tasks = data.get("tasks", [])
        for t in tasks:
            if t.get("id") == task_id:
                if args.get("status"):
                    valid = {"pending", "in_progress", "done", "failed", "skipped"}
                    if args["status"] not in valid:
                        return {"error": f"status must be one of {sorted(valid)}"}
                    t["status"] = args["status"]
                if args.get("title"):
                    t["title"] = args["title"]
                if args.get("instruction"):
                    t["instruction"] = args["instruction"]
                if "notes" in args:
                    t["notes"] = args["notes"] or None
                data["tasks"] = tasks
                _save(data)
                return {"updated": True, "task": t}
        return {"error": f"task id '{task_id}' not found"}

    async def _plan_remove(args: dict) -> dict:
        task_id = (args.get("id") or "").strip()
        if not task_id:
            return {"error": "id is required"}
        data  = _load()
        tasks = data.get("tasks", [])
        before = len(tasks)
        tasks  = [t for t in tasks if t.get("id") != task_id]
        if len(tasks) == before:
            return {"error": f"task id '{task_id}' not found"}
        data["tasks"] = tasks
        _save(data)
        return {"removed": True, "id": task_id, "total": len(tasks)}

    async def _plan_next(_args: dict) -> dict:
        data  = _load()
        tasks = data.get("tasks", [])
        _TERMINAL = ("done", "skipped", "in_progress")
        remaining = sum(1 for t in tasks if t.get("status") in ("pending", "failed"))
        # Resume an in_progress task first (crash recovery)
        for t in tasks:
            if t.get("status") == "in_progress":
                return {"task": t, "remaining": remaining}
        # First actionable task in array order (pending or failed); skip terminal statuses
        for idx, t in enumerate(tasks):
            if t.get("status") not in _TERMINAL:
                result: dict = {"task": t, "remaining": remaining - 1}
                if t.get("status") == "failed":
                    result["retry"] = True
                    result["warning"] = (
                        "This task PREVIOUSLY FAILED. Read its 'notes' field for the failure reason. "
                        "Use a different approach — do not repeat the same steps."
                    )
                # Warn if later tasks are already done/skipped (orphan signal)
                later_done = [u["id"] for u in tasks[idx + 1:] if u.get("status") in ("done", "skipped")]
                if later_done:
                    result["notice"] = (
                        f"Tasks {later_done} (later in the plan) are already done/skipped. "
                        "If this task is now obsolete, call plan_update(id, status='skipped') "
                        "instead of dispatching it."
                    )
                return result
        return {"task": None, "remaining": 0,
                "message": "All tasks are done, skipped, or failed — set done=true"}

    return {
        "plan_list":   _plan_list,
        "plan_add":    _plan_add,
        "plan_update": _plan_update,
        "plan_remove": _plan_remove,
        "plan_next":   _plan_next,
    }


def _sub_agent_system_prompt(task_type: str = "implement") -> str:
    ctx = _runtime_context()
    if task_type == "inspect":
        mode_header = (
            ctx
            + "You are an INSPECTOR agent. Your only job is to READ files and return a detailed analysis.\n\n"
            "## INSPECTION MODE — read-only\n\n"
            "DO NOT write any files. DO NOT call `file_edit` or `run_shell`.\n"
            "Every response MUST contain tool calls (reads). A response with no tool calls is ALWAYS wrong.\n"
            "Returning `success: true` with empty `files_written` is CORRECT for inspection tasks.\n\n"
            "## Workflow — follow this order\n\n"
            "1. Call `memory_list()` to see what has already been analysed for this project\n"
            "2. Call `read_prd` to read the Product Requirements Document (stored at `docs/PRD.md`; "
            "use the `read_prd` tool — do NOT use `read_file` to search for PRD.md yourself)\n"
            "3. Read every file mentioned in your task using `read_file`; call `memory_store` after each read\n"
            "4. Return a JSON summary of your findings — no file writes required\n\n"
            "## Output format\n\n"
            "Output JSON ONLY after reading all relevant files:\n"
            '{"summary": "detailed findings: what is implemented, what is missing, what needs attention", '
            '"files_written": [], "commands_run": [], "success": true, "blocker": null}\n\n'
            "If you cannot access a required file:\n"
            '{"summary": "what you found so far", "files_written": [], "commands_run": [], '
            '"success": false, "blocker": "specific reason you could not complete the inspection"}\n\n'
        )
        return mode_header
    return (
        ctx
        + "You are an EXECUTOR agent. Your only job is to call tools and write files.\n\n"
        "## CRITICAL — file_edit is the ONLY way to write files\n\n"
        "Writing a file means calling the `file_edit` tool. There is no other way.\n"
        "Describing a file in text does NOT write it. Mentioning a filename does NOT write it.\n"
        "If you say 'I created server/main.py' without calling `file_edit`, the file does not exist "
        "and your task will be marked FAILED and retried. Do NOT narrate — ACT.\n\n"
        "## NEVER invent tool arguments or fabricate results\n\n"
        "Only report something as done if you actually called the tool that does it. "
        "The orchestrator tracks every tool call — any file in `files_written` that was not "
        "produced by a real `file_edit` call will be detected and your task will be hard-failed.\n\n"
        "WRONG — fabricated (tool was never called, file does not exist on disk):\n"
        '  {"summary": "Implemented src/main.py and src/utils.py", '
        '"files_written": ["src/main.py", "src/utils.py"], "success": true}\n\n'
        "CORRECT — call the tool first, then summarise:\n"
        "  Step 1: call file_edit(path='src/main.py', content='# full file content here...')\n"
        "  Step 2: call file_edit(path='src/utils.py', content='# full file content here...')\n"
        "  Step 3: output the JSON summary AFTER both calls succeed:\n"
        '  {"summary": "Implemented src/main.py and src/utils.py", '
        '"files_written": ["src/main.py", "src/utils.py"], "success": true}\n\n'
        "Never pass invented or placeholder argument values to any tool. "
        "If you do not know the correct value for a required argument (e.g. a real file path, "
        "a real package name, an existing function name), read the relevant file first to find it — "
        "do not guess.\n\n"
        "## You are NOT a planner\n\n"
        "Do NOT describe what you intend to do. Do NOT summarise the task. Do NOT explain your approach.\n"
        "Do NOT output the final JSON before you have written all required files.\n"
        "Every response MUST contain tool calls. A response with no tool calls is ALWAYS wrong.\n"
        "Returning `success: true` with empty `files_written` is ALWAYS wrong — "
        "unless the Nothing-to-fix rule (below) explicitly applies.\n\n"
        "## Workflow — follow this order every time\n\n"
        "1. Call `memory_list()` ONCE at the very start — **do NOT call it again after the first time**\n"
        "2. Call `read_prd` to read the Product Requirements Document (stored at `docs/PRD.md`; "
        "use the `read_prd` tool — do NOT use `read_file` to hunt for PRD.md yourself)\n"
        "3. Read context — **maximum 2 rounds**: before each `read_file`, call `memory_search` first — "
        "if a good observation exists, skip the read. Otherwise call `read_file` on the specific files "
        "named in your task, or `list_files` once if you need to confirm a path. Do NOT browse directories "
        "repeatedly. If your task names the file to write, skip `list_files` entirely and go straight to writing. "
        "`read_file` always returns `total_lines` — if the file is large, request specific ranges with "
        "`start_line`/`end_line` (e.g. `read_file(path='foo.py', start_line=200, end_line=400)`).\n"
        "   After each `read_file`, call `memory_store` with your key findings.\n"
        "4. Write files **one at a time** using `file_edit`. If your task involves multiple files, "
        "implement them strictly in sequence: write file 1 completely → call `memory_store` → "
        "write file 2 completely → call `memory_store` → and so on. "
        "Never plan or describe all files first — just write the first file, then the next.\n"
        "   **After each `file_edit` call succeeds, do NOT read that file back** — the write is confirmed. "
        "Re-reading a file you just wrote wastes rounds and triggers the stall detector.\n"
        "5. Run any required commands (install dependencies, build, test) using `run_shell`\n"
        "6. Return a JSON summary AFTER all files are written — not before\n\n"
        "## Memory tools\n\n"
        "You have three memory tools that persist observations across all agents working on this project:\n\n"
        "- `memory_list()` — see all files already analysed. **Call this ONCE at the very start only — never again.**\n"
        "- `memory_search(query)` — semantic search over stored observations. "
        "Call this BEFORE reading any file — if a useful observation exists you may not need to read the file.\n"
        "- `memory_store(file_path, observation)` — store your analysis AFTER reading OR WRITING a file. "
        "Write what the file does, key exported function/class names, patterns, and anything non-obvious. "
        "Do NOT store raw file content — store your own analysis (50–200 words). "
        "**Keep the observation under 200 characters** — longer strings cause a backend parser error (HTTP 500).\n\n"
        "**Read rule:** Before reading any file, call `memory_search` first. "
        "If a good observation already exists, skip the read.\n"
        "**Write rule:** After writing any file, call `memory_store` immediately with: the file's purpose, "
        "key exported names, what it owns. Keep the observation under 200 characters total — "
        "longer strings cause a backend error. This is how future agents find and reuse your work.\n"
        "**Anti-duplication rule:** Before creating any new module or utility, call "
        "`memory_search('[functionality]')` to check if an existing file already implements it. "
        "If memory returns a relevant observation, IMPORT from that file — never reimplement. "
        "Example: before writing auth helpers, search 'authentication tokens'; before writing API "
        "client code, search 'HTTP requests API client'. If you find an existing implementation, "
        "your task instruction should say 'use `path/to/existing.py` — do NOT recreate its functions.'\n\n"
        "## Web search\n\n"
        "You have `web_search` and `fetch_webpage` available. Use them when:\n"
        "- You are unsure of the correct API, import path, or configuration for a library\n"
        "- A `run_shell` command fails repeatedly and you need to diagnose the error\n"
        "- You need to verify the correct package name, version, or peer dependency\n"
        "Use specific, technical queries (e.g. 'vite 5 react plugin peer dependency', "
        "'express cors middleware options'). Do not search for things you already know.\n\n"
        "## Image generation\n\n"
        "You have a `generate_image` tool that calls a local AI image generator (ComfyUI). "
        "Use it whenever your task involves visual assets — do not skip image creation or use placeholder text.\n\n"
        "When to call it:\n"
        "- The project needs a logo, app icon, favicon, or brand image\n"
        "- A page has a hero section, background, banner, or decorative image\n"
        "- A component references an image path that does not exist yet\n"
        "- The PRD or task mentions images, illustrations, or visual design\n\n"
        "How to call it:\n"
        "`generate_image(prompt='detailed description of the image', output_path='public/images/hero.png')`\n"
        "Write a detailed, specific prompt — style, colours, mood, subject. "
        "Use the exact path the HTML/CSS already references. "
        "Never use `file_edit` to write content to `.png`, `.jpg`, or `.webp` paths — it will be rejected.\n\n"
        "## Folder layout — pick once, never mix\n\n"
        "Before writing your first file, determine the source layout this project uses:\n"
        "- Check for `TECH_DECISIONS.md` — it records the layout under `Source layout:`\n"
        "- If not present yet, pick based on the framework: Vite/React/Vue/Svelte → `src/`; "
        "Flask/FastAPI/Django → project root or a named package; .NET → whatever `dotnet new` creates\n\n"
        "**CRITICAL**: Once you decide, every source file in this task uses the SAME layout. "
        "If you write `src/main.py`, ALL other source files must also go under `src/`. "
        "If you write `main.py` at the root, do NOT also create `src/utils.py`. "
        "Config/build files (`package.json`, `tsconfig.json`, `.env`, `pyproject.toml`, `.gitignore`) "
        "always stay at the project root regardless of the source layout choice. "
        "Mixed layouts (some files under `src/`, some at root) are a hard failure.\n\n"
        "## File writing rules\n\n"
        "- Write complete file content — never truncate, use ellipsis, or leave TODO/FIXME/placeholder text\n"
        "- Returning `success: true` with any stub, TODO, FIXME, `raise NotImplementedError`, "
        "placeholder comment, or truncated section is a hard failure — the orchestrator will reject it\n"
        "- **Prefer small, focused files over large monolithic ones.** If a file would exceed ~150 lines, "
        "split it into logical modules (e.g. separate utils, hooks, components, constants). "
        "Smaller files are easier to write completely in one pass and less likely to be truncated.\n"
        "- All paths passed to `file_edit` must be relative to the OUTPUT DIRECTORY — never prefix them with the project name or any parent folder\n"
        "- Parent directories are created automatically; never use mkdir\n"
        "- For other binary files (fonts, videos, etc.) skip them and note it in your summary\n"
        "- Use `delete_path` to remove deprecated or unused files/directories — do not leave dead code\n"
        "- **Never import from the file you are currently writing** — this creates a circular self-import "
        "that crashes at runtime (a module must not import itself)\n"
        "- **Before writing any cross-file import**, call `grep_files` to confirm the imported name is "
        "actually defined/exported in the target module. Do not assume a name exists — verify it. "
        "If it does not exist, create the symbol in the target file first, then import it.\n\n"
        "## Service registration — SERVICES.json\n\n"
        "If your task creates any runnable process a user needs to start — an HTTP server, API, "
        "frontend dev server, CLI entry point, WebSocket server, etc. — you MUST write or update "
        "`SERVICES.json` at the project root. Read the existing file first (if present) and preserve "
        "other entries. Write the complete updated file using `file_edit` with path `SERVICES.json`.\n\n"
        "Schema:\n"
        "```json\n"
        '{"services": [{\n'
        '  "name": "backend",\n'
        '  "entry_file": "backend/main.py",\n'
        '  "start_command": "uvicorn main:app --host 0.0.0.0 --port 8000",\n'
        '  "port": 8000,\n'
        '  "install_command": "pip install -r requirements.txt",\n'
        '  "env_file": ".env"\n'
        "}]}\n"
        "```\n"
        "Fields: `name` (short id), `entry_file` (relative path), `start_command` (exact command to run), "
        "`port` (null if not a network service), `install_command` (null if no install step), "
        "`env_file` (null if none). Do NOT register build steps, test runners, or one-off scripts.\n\n"
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
        "- Never run servers or long-running processes (`npm run dev`, `npm start`, `python -m uvicorn`, etc.)\n"
        "- **Never run `npm install`, `yarn install`, `pnpm install`, `pip install`, or any package manager install "
        "command unless your task instruction EXPLICITLY tells you to.** When multiple sub-agents run in parallel, "
        "concurrent installs collide and corrupt the node_modules / virtualenv. The orchestrator assigns install "
        "responsibility to exactly one task — if yours does not mention it, skip it.\n"
        "- If a command fails, read the error and fix the root cause before retrying\n"
        "- If the same command fails twice with the same error, stop and report as a blocker\n\n"
        "## Nothing-to-fix rule (narrow exception only)\n\n"
        "This rule applies ONLY when your task explicitly asks you to FIX or REPAIR something AND "
        "you read the file and find it is already correct (e.g. the reported line number is beyond the file's end). "
        "In that case only, output success with empty files_written:\n"
        '{"summary": "No issue found — file already correct", "files_written": [], "commands_run": [], "success": true, "blocker": null}\n\n'
        "Do NOT apply this rule to skip creating new files. If your task says to CREATE a file, you must write it.\n\n"
        "## Output format\n\n"
        "Output JSON ONLY after all required files have been written — no prose, no fences:\n"
        '{"summary": "what you built", "files_written": ["path1", "path2"], '
        '"commands_run": ["cmd1"], "success": true, "blocker": null}\n\n'
        "If blocked by a missing external dependency, service, or user decision:\n"
        '{"summary": "what you attempted", "files_written": [], "commands_run": [], '
        '"success": false, "blocker": "specific description of what is blocking"}'
    )


def _sub_agent_user_prompt(
    task_instruction: str, output_dir: str, idea_name: str,
    prior_failures: list[dict] | None = None,
    source_root: str | None = None,
    task_type: str = "implement",
    interface_summary: str | None = None,
    memory_context: str | None = None,
) -> str:
    prior_block = ""
    if prior_failures:
        sections = []
        for f in prior_failures:
            handoff = (f.get("handoff") or "").strip()
            files = f.get("files_written") or []
            section = f"### Attempt {f['attempt']}\n\n{handoff}" if handoff else (
                f"### Attempt {f['attempt']}\n\n"
                f"FAILED BECAUSE: {f['blocker'] or f['summary'] or 'unknown'}"
            )
            if files:
                file_list = "\n".join(f"  - {fp}" for fp in files)
                section += (
                    f"\n\nFiles already written to disk by this attempt — "
                    f"read them before deciding what to do; do NOT rewrite them unless they are broken:\n{file_list}"
                )
            sections.append(section)
        prior_block = (
            "\n\n## Previous attempts — partial progress\n\n"
            + "\n\n---\n\n".join(sections)
            + "\n\n**Your job**: continue from where the last attempt left off. "
            "Read the files already on disk first. Only rewrite a file if it is broken or incomplete. "
            "Do not repeat the same failing approach."
        )
    structure_hint = ""
    if source_root and task_type != "inspect":
        structure_hint = (
            f"\n\n## ⚠ MANDATORY: source root is `{source_root}/`\n\n"
            f"Every source file you write MUST be placed under `{source_root}/`. "
            f"Writing ANY source file to the project root or to a directory other than `{source_root}/` "
            f"is a hard error — it breaks the project layout established by the scaffold. "
            f"Correct: `{source_root}/server/main.py`. Wrong: `server.py`, `server/main.py`, `main.py`."
        )
    ownership_block = ""
    if interface_summary and task_type != "inspect":
        ownership_block = (
            f"\n\n## Existing module map — DO NOT REIMPLEMENT\n\n"
            f"{interface_summary}\n\n"
            "These modules already exist. Before creating any new file, search memory for its topic. "
            "If memory or the map above shows a module that covers what you need, IMPORT from it "
            "instead of recreating the same logic. Writing a duplicate implementation is a hard failure."
        )
    memory_block = ""
    if memory_context and task_type != "scaffold":
        memory_block = (
            f"\n\n## Prior agent observations\n\n"
            f"These files have already been analysed by other agents on this project. "
            f"Use them to orient yourself — call `read_file` only if you need the actual code.\n\n"
            f"{memory_context}"
        )
    if task_type == "inspect":
        closing = (
            "Read the PRD for context, then read every file listed in your task. "
            "Return a JSON summary describing the current implementation state, what is complete, "
            "and what is missing or broken. Do NOT write any files."
        )
        prd_line = (
            f"REQUIREMENTS: call `read_prd` to read the Product Requirements Document for context "
            f"(stored at `docs/PRD.md`; the `read_prd` tool returns it directly — do NOT search for the file).\n"
        )
    elif task_type == "scaffold":
        closing = (
            "Your goal is a project skeleton that builds — not a feature implementation.\n"
            "Run any framework CLI commands listed in your task, then write only the minimal files "
            "needed for the build to pass (entry point, top-level config, dependency manifest). "
            "Do NOT implement business logic, domain models, or application features — "
            "those belong in later implement tasks.\n"
            "After completing, run the build command and confirm it exits cleanly. "
            "If it fails, fix the root cause before returning."
        )
        prd_line = ""  # scaffold tasks must not be pulled into PRD feature implementation
    else:
        closing = (
            "Start by reading the PRD (including the Module Interface Contract section) and any files "
            "needed for context, then write all required files, run any specified commands.\n"
            "Before returning:\n"
            "1. Call `read_file` on everything you wrote and fix any file that contains "
            "TODO/FIXME/placeholder text, stub implementations, or imports that don't match the contract. "
            "Every function body must contain real logic — `pass`, `return None`, `return {}`, "
            "`return []`, or `...` as the ONLY body statement is a stub and must be fixed.\n"
            "2. Run the language's per-file syntax check on each file you wrote "
            "(the checker the project's language and toolchain provide). Fix any errors.\n"
            "3. Run the project build command (`npm run build`, `cargo check`, `python -c 'import <module>'`, "
            "etc.) to confirm your changes integrate cleanly with the rest of the project. "
            "If the build fails, read the errors and fix the root cause — do NOT return until the build passes.\n"
            "4. After writing each file, call `memory_store` with the file's purpose and exported names.\n"
            "Return the JSON summary only after all files pass self-verification and the build is clean."
        )
        prd_line = (
            f"REQUIREMENTS: call `read_prd` to read the full Product Requirements Document "
            f"(stored at `docs/PRD.md`; the `read_prd` tool returns it directly — do NOT use read_file to "
            f"search for PRD.md) — do this before implementing and again after to verify compliance.\n"
            f"INTERFACE CONTRACT: The PRD contains a 'Module Interface Contract' section listing the exact "
            f"exports, prop names, and function signatures every module must implement. "
            f"Your files MUST match the contract — do not rename exports or change prop names.\n"
        )
    return (
        f"PROJECT: {idea_name}\n"
        f"OUTPUT DIRECTORY: {output_dir}\n"
        f"{prd_line}"
        f"{structure_hint}"
        f"{ownership_block}"
        f"{memory_block}"
        f"\n\n## Your task\n\n{task_instruction}"
        f"{prior_block}\n\n"
        f"{closing}"
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
        register_sub_agent_task: "Callable[[str, asyncio.Task], None] | None" = None,
        is_sub_agent_user_cancelled: "Callable[[str], bool] | None" = None,
    ) -> str:
        from app import telemetry as _telemetry
        _tech_type = _detect_tech_type(prd_content)
        _telemetry.set_project(idea.id, idea.name, _tech_type)

        output_dir = session.output_dir or ""
        completed_tasks: list[dict] = []
        _interface_summary: str | None = None  # updated after every batch
        # Only inject follow-up context on the first round
        _initial_follow_up = follow_up_message
        _task_perm_fail_counts: dict[str, int] = {}  # per-task-id count of permanent failures
        _MAX_TASK_PERM_FAILS = 2      # block re-dispatch after this many permanent failures per task
        _MAX_ALL_FAILED_ROUNDS = 8    # abort run after this many consecutive all-failed rounds

        # Detect resume vs. fresh start by checking whether real project files already exist.
        # Internal files (.think-plan.json, PROGRESS.md, etc.) don't count.
        _INTERNAL_FILES = {".think-plan.json", "PROGRESS.md", ".think-memory.json"}
        _is_resume = False
        if output_dir and Path(output_dir).exists():
            _has_project_files = any(
                p for p in Path(output_dir).rglob("*")
                if p.is_file() and p.name not in _INTERNAL_FILES and not p.name.endswith(".log")
            )
            _is_resume = _has_project_files

        _plan_file = Path(output_dir) / ".think-plan.json" if output_dir else None
        if _is_resume and _plan_file and _plan_file.exists():
            # Resume: load completed/failed tasks from the plan so the orchestrator knows
            # what was already done and doesn't restart from scaffolding.
            try:
                _plan_data = json.loads(_plan_file.read_text(encoding="utf-8"))
                _pending_plan_tasks: list[dict] = []
                for _pt in _plan_data.get("tasks", []):
                    _status = _pt.get("status", "")
                    if _status == "done":
                        completed_tasks.append({
                            "id": _pt["id"],
                            "title": _pt.get("title", _pt["id"]),
                            "success": True,
                            "summary": _pt.get("summary") or _pt.get("notes") or "Completed in a previous run.",
                        })
                    elif _status == "failed":
                        completed_tasks.append({
                            "id": _pt["id"],
                            "title": _pt.get("title", _pt["id"]),
                            "success": False,
                            "permanently_failed": True,
                            "summary": _pt.get("notes") or "Failed in a previous run.",
                        })
                        # Pre-veto so the orchestrator cannot re-dispatch the same task unchanged
                        _task_perm_fail_counts[_pt["id"]] = _MAX_TASK_PERM_FAILS
                    elif _status in ("pending", "in_progress"):
                        _pending_plan_tasks.append(_pt)

                # If the plan had no done/failed tasks (e.g. run stopped before any task
                # completed, or plan was written but statuses were never updated), inject
                # a synthetic scaffold entry so the orchestrator knows the project exists
                # and doesn't restart from Milestone 0. Include the pending plan tasks in
                # the summary so the orchestrator picks up its own prior plan.
                if not completed_tasks:
                    _disk_files = sorted(
                        p.relative_to(Path(output_dir)).as_posix()
                        for p in Path(output_dir).rglob("*")
                        if p.is_file()
                        and p.name not in _INTERNAL_FILES
                        and not p.name.endswith((".log", ".pyc"))
                        and "__pycache__" not in p.parts
                    )
                    _pending_titles = [_pt.get("title", _pt["id"]) for _pt in _pending_plan_tasks]
                    _resume_summary = (
                        "Resumed from a previous run. The project is partially implemented — do NOT re-initialize or re-scaffold. "
                        f"Files already on disk: {', '.join(_disk_files[:20])}"
                        + (f" (+{len(_disk_files) - 20} more)" if len(_disk_files) > 20 else "")
                        + ". "
                        + (f"Pending plan tasks from last run: {'; '.join(_pending_titles)}. " if _pending_titles else "")
                        + "Call plan_list() to see the current plan, then dispatch the next pending task immediately. "
                        "Do not inspect files or call run_build before dispatching — pick up where the previous run stopped."
                    )
                    completed_tasks.append({
                        "id": "_resume_context",
                        "title": "Resumed from previous run — project partially implemented",
                        "success": True,
                        "summary": _resume_summary,
                        "files_written": _disk_files[:10],
                    })

                logger.info(
                    "orchestrator: resume detected — %d completed task(s) from plan, %d pending",
                    len([t for t in completed_tasks if t["id"] != "_resume_context"]),
                    len(_pending_plan_tasks),
                )
            except Exception as _e:
                logger.warning("orchestrator: could not load plan for resume: %s", _e)
        elif _plan_file and _plan_file.exists():
            # Fresh start but a stale plan file exists — delete it.
            try:
                _plan_file.unlink()
                logger.info("orchestrator: deleted stale plan file (fresh start)")
            except Exception as _e:
                logger.warning("orchestrator: could not delete stale plan file: %s", _e)

        _verification_pending = False  # True after first done=true; forces explicit PRD check rounds
        _verification_attempts = 0    # counts how many verification rounds have run
        _empty_task_rounds = 0        # consecutive rounds with done=false but no tasks produced
        _consecutive_failures = 0     # consecutive rounds that raised an exception
        _all_failed_rounds = 0        # consecutive rounds where every dispatched task permanently failed
        _prd_sections = _extract_prd_sections(prd_content)

        # Research current framework versions before the first round (fresh starts only).
        # The result is injected into the system prompt every round so the orchestrator
        # always has live version data when writing task instructions.
        _framework_research: str | None = None
        if _tech_type and not _is_resume:
            logger.info("orchestrator: researching framework %r before round 0", _tech_type)
            _framework_research = await _research_framework(_tech_type)
            if _framework_research:
                logger.info("orchestrator: framework research retrieved (%d chars)", len(_framework_research))

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
            _file_tree = _build_file_tree(output_dir) if output_dir else None
            _services_json = _read_services_json(output_dir) if output_dir else None
            orch_messages = [
                Message(role="system", content=_orchestrator_system_prompt(prd_content, _framework_research)),
                Message(role="user", content=_orchestrator_user_prompt(
                    idea, branch, completed_tasks, _initial_follow_up,
                    pending_user_messages=pending_messages or None,
                    verify_prd=_verification_pending,
                    prd_sections=_prd_sections if _verification_pending else None,
                    interface_summary=_interface_summary,
                    file_tree=_file_tree,
                    services_json=_services_json,
                    output_dir=output_dir or None,
                )),
            ]
            _initial_follow_up = None  # only include on first round

            # Periodic build check — runs every N rounds after tasks have been written
            _impl_rounds = sum(1 for t in completed_tasks if not t["id"].startswith("_"))
            _last_build_idx = max(
                (i for i, t in enumerate(completed_tasks) if t["id"].startswith("_build_check_")),
                default=-1,
            )
            _files_since_build = any(
                t.get("files_written")
                for t in completed_tasks[_last_build_idx + 1:]
                if not t["id"].startswith("_")
            )
            if (
                not _verification_pending
                and _impl_rounds > 0
                and round_idx > 0
                and round_idx % _BUILD_CHECK_INTERVAL == 0
                and _files_since_build  # skip if no files written since last build — would always fail
            ):
                logger.info("orchestrator: running periodic build check at round %d", round_idx + 1)
                await on_orchestrator_event("orchestrator_message", {
                    "content": f"🔨 Build check (round {round_idx + 1}): running…"
                })
                build_result = await _run_build(output_dir)
                if build_result.get("success") is False:
                    build_out = build_result.get("output", "")[:2000]
                    await on_orchestrator_event("orchestrator_message", {
                        "content": f"🔨 Build check (round {round_idx + 1}): **BUILD FAILED**\n```\n{build_out}\n```"
                    })
                    completed_tasks.append({
                        "id": f"_build_check_{round_idx}",
                        "title": "(periodic build check failed)",
                        "summary": (
                            f"Build command '{build_result.get('command', 'unknown')}' failed "
                            f"(exit {build_result.get('exit_code', -1)}):\n\n{build_out}\n\n"
                            "Fix all build errors before continuing with new features. "
                            "Each error message shows the exact file and line that is broken."
                        ),
                        "success": False,
                        "files_written": [],
                        "commands_run": [],
                    })
                elif build_result.get("success") is True:
                    await on_orchestrator_event("orchestrator_message", {
                        "content": f"✅ Build check (round {round_idx + 1}): build passes"
                    })
                    completed_tasks.append({
                        "id": f"_build_check_{round_idx}",
                        "title": "(periodic build check passed)",
                        "summary": f"Build command '{build_result.get('command', 'unknown')}' succeeded.",
                        "success": True,
                        "files_written": [],
                        "commands_run": [],
                    })

            async def _orch_tool_cb(tool: str, result: dict) -> None:
                await on_orchestrator_event("orchestrator_tool", {"tool": tool, "result": result})

            # Token-level streaming from OpenVINO — shows orchestrator reasoning in the UI.
            # Called from a thread pool thread, so we schedule back onto the event loop.
            _orch_stream_loop = asyncio.get_running_loop()
            _stream_in_think = False
            _stream_buf: list[str] = []
            _think_log: list[str] = []

            def _orch_on_token(token: str) -> None:
                nonlocal _stream_in_think, _stream_buf
                _stream_in_think, visible = _extract_think_content(token, _stream_in_think)
                if visible:
                    _think_log.append(visible)
                    _stream_buf.append(visible)
                    chunk = "".join(_stream_buf)
                    _stream_buf.clear()
                    asyncio.run_coroutine_threadsafe(
                        on_orchestrator_event("orchestrator_token", {"content": chunk}),
                        _orch_stream_loop,
                    )

            async def _handle_inspect_files(args: dict) -> dict:
                raw_paths = [str(p) for p in (args.get("paths") or []) if p][:10]
                focus = str(args.get("focus") or "")
                paths = _expand_inspection_paths(output_dir, raw_paths, max_files=10)
                if not paths:
                    return {"files": [], "error": "no paths provided"}
                return await self._run_inspector_agent(
                    output_dir, paths, focus, on_orchestrator_event
                )

            async def _handle_run_build(args: dict) -> dict:
                cmd = str(args.get("command") or "").strip() or None
                await on_orchestrator_event("orchestrator_message", {
                    "content": f"🔨 Running build{f': `{cmd}`' if cmd else ''}…"
                })
                return await _run_build(output_dir, cmd)

            async def _handle_scaffold_misuse(_args: dict) -> dict:
                return {"error": (
                    "scaffold is not a callable tool — stop calling it. "
                    "To scaffold the project, end this round by returning your final JSON right now:\n"
                    '{"analysis":"<why scaffold is needed>","tasks":[{"task_type":"scaffold",'
                    '"id":"scaffold_1","title":"Set up project structure",'
                    '"instruction":"<detailed instruction for the sub-agent>"}],"done":false}'
                )}

            async def _handle_list_files(args: dict) -> dict:
                # Serve list_files from the pre-built tree — no filesystem round-trips,
                # no subdirectory drilling. The model gets exactly what it asked for
                # (files under a given path) without burning extra tool rounds.
                req_path = str(args.get("path") or ".").strip().rstrip("/")
                if not _file_tree:
                    return {"path": req_path, "entries": [], "count": 0}
                prefix = "" if req_path == "." else req_path + "/"
                entries = []
                seen_dirs: set[str] = set()
                for line in _file_tree.splitlines():
                    if not line or line.startswith("…"):
                        continue
                    if req_path == ".":
                        # top-level: return immediate children only
                        top = line.split("/")[0]
                        if "/" in line:
                            if top not in seen_dirs:
                                seen_dirs.add(top)
                                entries.append(top + "/")
                        else:
                            entries.append(line)
                    else:
                        if not line.startswith(prefix):
                            continue
                        rest = line[len(prefix):]
                        if "/" in rest:
                            d = rest.split("/")[0]
                            if d not in seen_dirs:
                                seen_dirs.add(d)
                                entries.append(d + "/")
                        else:
                            entries.append(rest)
                return {"path": req_path, "entries": sorted(set(entries)), "count": len(entries)}

            try:
                from app.memory import MEMORY_SEARCH_TOOL, MEMORY_LIST_TOOL
                # During Milestone 0 (no completed tasks) suppress plan tools and
                # run_build — both cause the model to spin planning/checking before
                # there is any scaffold to build or plan against.
                # Resumed sessions always have files on disk even though loaded plan tasks
                # don't carry files_written — never treat a resume as Milestone 0.
                _is_milestone_0 = not _is_resume and not any(r.get("files_written") for r in completed_tasks if isinstance(r, dict))
                _extra_tools = [INSPECT_FILES_TOOL, MEMORY_SEARCH_TOOL, MEMORY_LIST_TOOL]
                _extra_handlers: dict = {
                    "inspect_files": _handle_inspect_files,
                    "scaffold": _handle_scaffold_misuse,
                    **_memory_handlers(idea.id),
                }
                if not _is_milestone_0:
                    _extra_tools += [
                        _RUN_BUILD_TOOL,
                        _PLAN_LIST_TOOL, _PLAN_ADD_TOOL, _PLAN_UPDATE_TOOL,
                        _PLAN_REMOVE_TOOL, _PLAN_NEXT_TOOL,
                    ]
                    _extra_handlers["run_build"] = _handle_run_build
                    _extra_handlers.update(_plan_handlers(output_dir))
                orch_result = await self._client.call_with_tools(
                    stage_key="phase3_verification" if _verification_pending else "phase3_orchestrator",
                    messages=orch_messages,
                    session=db,
                    idea_id=idea.id,
                    branch_id=branch.id,
                    allowed_file_dir=output_dir,
                    explore_only=True,
                    # 20 rounds: inspect_files (up to 3 batches of 10 files) +
                    # grep/list (2-3) + build check (1) + dispatch JSON (1) + spare.
                    # Verification: 25 rounds — must inspect every file then classify each PRD section.
                    max_tool_rounds=25 if _verification_pending else 12,
                    return_json=True,
                    call_index=0,
                    on_tool_result=_orch_tool_cb,
                    extra_tools=_extra_tools,
                    custom_tool_handlers=_extra_handlers,
                    on_token=_orch_on_token,
                )
                if _think_log:
                    logger.debug("orchestrator think (round %d):\n%s", round_idx + 1, "".join(_think_log))
                    _think_log.clear()
            except Exception as e:
                logger.error("orchestrator: round %d failed: %s", round_idx + 1, e)
                _consecutive_failures += 1
                # Clear the thinking spinner so the UI doesn't get stuck
                await on_orchestrator_event("orchestrator_message", {
                    "content": f"⚠ Orchestrator round failed: {e}. Retrying…"
                })
                if _consecutive_failures <= 2:
                    logger.warning("orchestrator: transient failure #%d — retrying round", _consecutive_failures)
                    if "exceeded max tool rounds" in str(e):
                        completed_tasks.append({
                            "id": f"_round_limit_{round_idx}",
                            "title": "(round limit hit — stop exploring, dispatch tasks now)",
                            "summary": (
                                "You spent all available rounds calling list_files/inspect_files "
                                "without returning any tasks. "
                                "On your NEXT response you MUST return a JSON object with 1–2 concrete "
                                "implementation tasks immediately — no tool calls first. "
                                "If the project is empty, dispatch scaffold tasks. "
                                "If files exist, dispatch the next implementation tasks. "
                                "Do NOT call list_files or inspect_files again."
                            ),
                            "success": False,
                            "files_written": [],
                            "commands_run": [],
                        })
                    continue
                logger.error("orchestrator: %d consecutive failures — stopping", _consecutive_failures)
                break
            else:
                _consecutive_failures = 0  # reset on success

            if not isinstance(orch_result, dict):
                logger.warning("orchestrator: non-dict result in round %d", round_idx + 1)
                await on_orchestrator_event("orchestrator_message", {
                    "content": "⚠ Orchestrator returned an unexpected response — retrying."
                })
                continue

            user_message, done, next_tasks = _parse_orchestrator_result(orch_result)

            logger.info("orchestrator: done=%s tasks=%d question=%s",
                        done, len(next_tasks), bool(user_message))

            # Enforce plan-first discipline: if the plan file is still empty/missing
            # and the model skipped straight to dispatching tasks, reject the batch
            # and force a plan-build round. Instruction-only approaches fail because
            # small models return final JSON in round 1 without making any tool calls.
            if (
                next_tasks
                and not done
                and not user_message
                and not _verification_pending
                and not follow_up_message
                and (_is_resume or any(r.get("files_written") for r in completed_tasks if isinstance(r, dict)))  # scaffold exists — not Milestone 0
                and output_dir
            ):
                _pf = Path(output_dir) / ".think-plan.json"
                _plan_currently_empty = True
                if _pf.is_file():
                    try:
                        _pd = json.loads(_pf.read_text(encoding="utf-8"))
                        _plan_currently_empty = len(_pd.get("tasks", [])) == 0
                    except Exception:
                        pass
                if _plan_currently_empty:
                    logger.warning(
                        "orchestrator: round %d dispatched %d task(s) without building a plan — rejecting and forcing plan build",
                        round_idx + 1, len(next_tasks),
                    )
                    completed_tasks.append({
                        "id": f"_plan_required_{round_idx}",
                        "title": "(plan required — tasks rejected)",
                        "summary": (
                            "REJECTED: You dispatched tasks without calling plan_list() or plan_add() first. "
                            "You MUST build the plan before dispatching anything. "
                            "Call plan_list() now. If it returns no tasks, call plan_add() for every PRD "
                            "feature before including anything in next_tasks."
                        ),
                        "success": False,
                        "files_written": [],
                        "commands_run": [],
                    })
                    continue

            # Orchestrator needs blocking user input before it can continue
            if user_message:
                content = str(user_message).strip()
                # Intercept capability complaints — these are orchestrator bugs, not genuine questions.
                # The model realised it lacks a tool and punted to the user instead of dispatching a task.
                _CAPABILITY_PHRASES = (
                    "not available in this environment",
                    "run_shell is not available",
                    "cannot run shell",
                    "tool is not available",
                    "don't have access to",
                    "do not have access to",
                    "unable to run",
                    "need to handle",
                    "manually",
                )
                if any(p in content.lower() for p in _CAPABILITY_PHRASES):
                    logger.warning(
                        "orchestrator: user_message looks like a capability complaint — nudging to dispatch a task instead: %r",
                        content[:120],
                    )
                    completed_tasks.append({
                        "id": f"_capability_complaint_{round_idx}",
                        "title": "(capability complaint intercepted)",
                        "summary": (
                            "REJECTED: Do not ask the user to run commands. "
                            "Sub-agents have run_shell — dispatch a task with the shell command in its instruction. "
                            "Issue a JSON response with next_tasks now."
                        ),
                        "success": False,
                        "files_written": [],
                        "commands_run": [],
                    })
                    continue
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
                    # First time done=true: enter PRD verification mode
                    _verification_pending = True
                    logger.info("orchestrator: done=true signalled — entering PRD verification mode")
                    continue

                if done and _verification_pending:
                    # Verification round returned done=true — check if analysis still contains
                    # signs of incomplete work before accepting
                    _verification_attempts += 1
                    analysis_text = str(orch_result.get("analysis") or "").lower()
                    _INCOMPLETE_SIGNALS = ("partial", "missing", "not implemented", "stub", "todo",
                                           "incomplete", "not built", "placeholder", "not done")
                    incomplete_signals_found = [s for s in _INCOMPLETE_SIGNALS if s in analysis_text]
                    # Also reject if the analysis has no evidence quotes at all — the model
                    # classified sections without actually reading the code.
                    has_evidence = "evidence:" in analysis_text
                    if not has_evidence and _prd_sections:
                        incomplete_signals_found = incomplete_signals_found or ["no evidence quotes found"]
                    if incomplete_signals_found and _verification_attempts <= 2:
                        logger.warning(
                            "orchestrator: verification round %d said done=true but analysis contains "
                            "incomplete signals %s — forcing another pass",
                            _verification_attempts, incomplete_signals_found,
                        )
                        completed_tasks.append({
                            "id": f"_verify_pushback_{round_idx}",
                            "title": "(verification rejected — incomplete sections found)",
                            "summary": (
                                f"Your analysis contained these problems: "
                                f"{', '.join(incomplete_signals_found)}. "
                                "You MUST call `inspect_files` on the actual implementation files "
                                "and quote specific function names or code as evidence for each section. "
                                "Sections with no quoted evidence are treated as MISSING. "
                                "Add tasks for every PARTIAL or MISSING section and set done=false."
                            ),
                            "success": False,
                            "files_written": [],
                            "commands_run": [],
                        })
                        continue
                    logger.info("orchestrator: verification complete after %d attempt(s)", _verification_attempts)

                if not done and not next_tasks:
                    # Orchestrator returned done=false but produced no tasks — nudge it
                    _empty_task_rounds += 1
                    if _empty_task_rounds <= 3:
                        logger.warning("orchestrator: round %d produced done=false with no tasks (empty #%d) — nudging",
                                       round_idx + 1, _empty_task_rounds)
                        from app import telemetry as _tel_orch
                        _stage_used = "phase3_verification" if _verification_pending else "phase3_orchestrator"
                        _stage_cfg_orch = self._client._registry.get_stage(_stage_used)
                        _tel_orch.log_call(
                            stage=_stage_used,
                            model=_stage_cfg_orch.model,
                            backend=_stage_cfg_orch.backend,
                            duration_ms=None,
                            success=False,
                            error=f"no tasks returned (empty #{_empty_task_rounds})",
                            _ctx={},
                        )
                        if _impl_rounds == 0:
                            # Nothing has been written yet — project is empty or very early.
                            # Don't tell the model to call list_files again; that just wastes
                            # another round. Force it to dispatch a concrete task immediately.
                            _nudge_summary = (
                                "You returned done=false but produced no tasks, and no files have been written yet. "
                                "Do NOT call list_files, inspect_files, or any other tool. "
                                "Output a JSON object RIGHT NOW with at least one concrete task in next_tasks. "
                                "If the project is empty, your first task must be a scaffold task that creates the initial project structure."
                            )
                        else:
                            _nudge_summary = (
                                "You returned done=false but gave no next_tasks. "
                                "Inspect any files you haven't reviewed yet, then output concrete implementation tasks."
                            )
                        completed_tasks.append({
                            "id": f"_nudge_{round_idx}",
                            "title": "(no tasks produced)",
                            "summary": _nudge_summary,
                            "success": False,
                            "files_written": [],
                            "commands_run": [],
                        })
                        continue
                    logger.warning("orchestrator: %d consecutive empty-task rounds — stopping", _empty_task_rounds)
                logger.info("orchestrator: signalled done after %d round(s)", round_idx + 1)
                break

            # Validate tasks — reject empty instructions and exploration-only tasks
            _EXPLORE_PREFIXES = ('list', 'read', 'inspect', 'explore', 'check', 'view', 'show', 'get', 'find')
            _IMPL_KEYWORDS = ('write', 'creat', 'implement', 'install', 'build', 'edit', 'modif', 'generat', 'add', 'setup', 'configure', 'scaffold')
            valid_tasks = []
            _blocked_this_round: list[str] = []
            for t in next_tasks[:3]:  # cap at 3 concurrent sub-agents
                task_id_raw = str(t.get("id") or "")
                instruction = str(t.get("instruction") or "").strip()
                if not instruction:
                    logger.warning("orchestrator: task %r has empty instruction — skipping", task_id_raw)
                    continue
                # Runtime veto: block tasks that have permanently failed too many times
                if _task_perm_fail_counts.get(task_id_raw, 0) >= _MAX_TASK_PERM_FAILS:
                    logger.warning(
                        "orchestrator: blocking vetoed task '%s' (%d permanent failures)",
                        task_id_raw, _task_perm_fail_counts[task_id_raw],
                    )
                    _blocked_this_round.append(task_id_raw)
                    continue
                explicit_inspect = str(t.get("task_type") or "").strip() == "inspect"
                title_lower = str(t.get("title") or "").lower().strip()
                instruction_lower = instruction.lower()
                is_explore_only = (
                    not explicit_inspect
                    and any(title_lower.startswith(p) for p in _EXPLORE_PREFIXES)
                    and not any(kw in instruction_lower for kw in _IMPL_KEYWORDS)
                )
                if is_explore_only:
                    logger.warning("orchestrator: task %r looks like exploration-only — skipping (use list_files/inspect_files directly)", t.get("title"))
                    continue
                valid_tasks.append(t)
            # Inject veto notices so the orchestrator knows why tasks were blocked
            if _blocked_this_round:
                completed_tasks.append({
                    "id": f"_veto_notice_{round_idx}",
                    "title": "⛔ Runtime veto — tasks blocked",
                    "summary": (
                        f"The runtime has PERMANENTLY BLOCKED these task IDs after repeated failures: "
                        f"{_blocked_this_round}. "
                        f"You MUST call plan_remove(id) for each blocked id, then plan_add 1-2 "
                        f"smaller replacement tasks each touching exactly ONE file. "
                        f"Do NOT re-dispatch any blocked id."
                    ),
                    "success": False,
                    "files_written": [],
                    "commands_run": [],
                })
            if not valid_tasks:
                logger.warning("orchestrator: no valid tasks in round %d — continuing to next round", round_idx + 1)
                continue  # let the orchestrator try again rather than stopping dead

            _empty_task_rounds = 0  # reset — real tasks are being dispatched

            # Emit a chat message summarising the plan before launching the batch
            analysis = str(orch_result.get("analysis") or "").strip()
            task_lines = "\n".join(
                f"- **{str(t.get('title') or 'Task')}**" for t in valid_tasks
            )
            n = len(valid_tasks)
            plan_msg = (
                f"{analysis}\n\n" if analysis else ""
            ) + (
                f"Launching {n} task{'s' if n > 1 else ''} in parallel:\n{task_lines}"
                if n > 1
                else f"Starting task:\n{task_lines}"
            )
            await on_orchestrator_event("orchestrator_message", {"content": plan_msg})

            # Emit queued events for all tasks upfront; started fires per-task once a slot is free
            for t in valid_tasks:
                task_id = str(t.get("id") or f"task_{round_idx}")
                task_title = str(t.get("title") or "Task")[:80]
                agent_id = uuid.uuid4().hex[:8]
                t["_id"] = task_id
                t["_title"] = task_title
                t["_agent_id"] = agent_id
                await on_orchestrator_event("sub_agent_queued", {"task_id": task_id, "title": task_title, "agent_id": agent_id})

            # Detect established source layout so sub-agents stay consistent
            _structure = _detect_project_structure(completed_tasks, output_dir or None)

            # Run all tasks in this batch concurrently
            batch_results = await self._run_task_batch(
                idea, branch, output_dir, valid_tasks,
                on_tool_result, on_orchestrator_event,
                source_root=_structure["source_root"],
                prd_content=prd_content,
                interface_summary=_interface_summary,
                register_task=register_sub_agent_task,
                is_user_cancelled=is_sub_agent_user_cancelled,
            )

            for t, sub_result in zip(valid_tasks, batch_results):
                completed_tasks.append({"id": t["_id"], "title": t["_title"], **sub_result})
                # Track per-task permanent failure count for runtime veto
                if sub_result.get("permanently_failed"):
                    _task_perm_fail_counts[t["_id"]] = _task_perm_fail_counts.get(t["_id"], 0) + 1
                    if _task_perm_fail_counts[t["_id"]] >= _MAX_TASK_PERM_FAILS:
                        logger.warning(
                            "orchestrator: task '%s' hit veto threshold (%d permanent failures) — "
                            "runtime will block future dispatch",
                            t["_id"], _task_perm_fail_counts[t["_id"]],
                        )

            # Auto-update plan file so plan_next stays accurate even when the orchestrator
            # model forgets to call plan_update after a task completes.
            # Uses plan_task_id (explicit link) if the orchestrator provided it,
            # then falls back to the dispatched task's id, then scans for orphaned
            # in_progress tasks as a last resort.
            import json as _json
            _plan_file = Path(output_dir) / ".think-plan.json"
            if _plan_file.exists():
                try:
                    _plan_data = _json.loads(_plan_file.read_text(encoding="utf-8"))
                    _plan_map = {t["id"]: t for t in _plan_data.get("tasks", [])}
                    _plan_dirty = False
                    _matched_plan_ids: set[str] = set()
                    for t, sub_result in zip(valid_tasks, batch_results):
                        # plan_task_id is the explicit link the orchestrator copied from plan_next();
                        # fall back to the dispatched task's own id if omitted.
                        _plan_lookup_id = t.get("plan_task_id") or t["_id"]
                        pt = _plan_map.get(_plan_lookup_id)
                        if pt is None:
                            # Also try the dispatched id in case orchestrator set plan_task_id wrong
                            pt = _plan_map.get(t["_id"])
                        if pt is None:
                            continue
                        _matched_plan_ids.add(pt["id"])
                        current = pt.get("status", "pending")
                        if sub_result.get("success") and current != "done":
                            pt["status"] = "done"
                            _plan_dirty = True
                            logger.debug("orchestrator: plan auto-done '%s' (via %s)", pt["id"], _plan_lookup_id)
                        elif sub_result.get("permanently_failed") and current not in ("done", "failed"):
                            pt["status"] = "failed"
                            _plan_dirty = True
                            logger.debug("orchestrator: plan auto-failed '%s'", pt["id"])

                    # Fallback: if a plan task was set to in_progress but the dispatched task
                    # used a completely different id (no match above), the plan task is orphaned.
                    # If this round had at least one success, mark the orphan done — the orchestrator
                    # would only mark a task in_progress immediately before dispatching it.
                    _round_had_success = any(r.get("success") for r in batch_results)
                    if _round_had_success:
                        for pt in _plan_data.get("tasks", []):
                            if pt.get("status") == "in_progress" and pt["id"] not in _matched_plan_ids:
                                pt["status"] = "done"
                                _plan_dirty = True
                                logger.info(
                                    "orchestrator: plan auto-done orphaned in_progress task '%s' "
                                    "(id mismatch — dispatched task used a different id)",
                                    pt["id"],
                                )

                    # Detect pending tasks that appear before a done task in the array —
                    # these were orphaned when the orchestrator skipped ahead in the queue.
                    # Log them so the next round's prompt can include a cleanup hint.
                    _plan_tasks_arr = _plan_data.get("tasks", [])
                    _last_done_idx = max(
                        (i for i, pt in enumerate(_plan_tasks_arr)
                         if pt.get("status") in ("done", "skipped")),
                        default=-1,
                    )
                    _orphaned_pending = [
                        pt["id"] for i, pt in enumerate(_plan_tasks_arr)
                        if i < _last_done_idx and pt.get("status") in ("pending", "failed")
                    ]
                    if _orphaned_pending:
                        logger.warning(
                            "orchestrator: orphaned pending task(s) before last done task — "
                            "plan_next will return these until cleaned up: %s",
                            _orphaned_pending,
                        )

                    if _plan_dirty:
                        _plan_file.write_text(
                            _json.dumps(_plan_data, indent=2, ensure_ascii=False), encoding="utf-8"
                        )
                except Exception as _pe:
                    logger.warning("orchestrator: plan auto-update error: %s", _pe)

            # Detect when every task in the batch permanently failed (all models exhausted)
            _batch_all_permanent = batch_results and all(r.get("permanently_failed") for r in batch_results)
            if _batch_all_permanent:
                _all_failed_rounds += 1
                logger.warning(
                    "orchestrator: %d consecutive all-failed batches — injecting decomposition nudge",
                    _all_failed_rounds,
                )
                completed_tasks.append({
                    "id": f"_all_failed_nudge_{round_idx}",
                    "title": "(all tasks failed — forced decomposition)",
                    "summary": (
                        f"CRITICAL: Every task in the last {_all_failed_rounds} rounds has permanently failed. "
                        "The current task scope is too large or the instruction is ambiguous. "
                        "You MUST change strategy: break the work into tasks that each touch exactly ONE file "
                        "and implement exactly ONE function or class. Do NOT re-dispatch any task whose id "
                        "already appears in the completed list. If the scaffold is missing, create one file at a time "
                        "starting with the entry point. Do not dispatch more than 1 task this round."
                    ),
                    "success": False,
                    "files_written": [],
                    "commands_run": [],
                })
                if _all_failed_rounds >= _MAX_ALL_FAILED_ROUNDS:
                    logger.error(
                        "orchestrator: %d consecutive all-failed rounds — aborting run (deadlock detected)",
                        _all_failed_rounds,
                    )
                    await on_orchestrator_event("orchestrator_message", {
                        "content": (
                            f"⛔ Run aborted: {_all_failed_rounds} consecutive rounds with no successful tasks. "
                            "All sub-agent models have been exhausted. Check the model configuration and logs."
                        )
                    })
                    break
            else:
                _all_failed_rounds = 0

            # Auto-store file ownership in memory so agents can find existing implementations
            import app.memory as _mem
            _ownership_stores = []
            for t, sub_result in zip(valid_tasks, batch_results):
                summary_snippet = (sub_result.get("summary") or "")[:300]
                for fp in (sub_result.get("files_written") or [])[:15]:
                    if fp:
                        obs = f"Written by task '{t.get('_title', '')}'. {summary_snippet}"
                        _ownership_stores.append(_mem.store(idea.id, fp, obs.strip()))
            if _ownership_stores:
                await asyncio.gather(*_ownership_stores, return_exceptions=True)

            # Update living interface manifest and PROGRESS.md after every batch
            _manifest_path = write_interface_manifest(output_dir)
            if _manifest_path:
                try:
                    _fresh_manifest = extract_interface(output_dir)
                    _interface_summary = format_manifest_summary(_fresh_manifest)
                except Exception as _exc:
                    logger.warning("orchestrator: interface summary rebuild failed: %s", _exc)
            _write_progress_md(output_dir, _prd_sections, completed_tasks, idea.name, round_idx)

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
        source_root: str | None = None,
        prd_content: str | None = None,
        interface_summary: str | None = None,
        register_task: "Callable[[str, asyncio.Task], None] | None" = None,
        is_user_cancelled: "Callable[[str], bool] | None" = None,
    ) -> list[dict]:
        """Run a batch of tasks concurrently. Each task gets its own DB session."""

        max_verify_cycles = self._client._registry.resources.max_verify_fix_cycles

        async def _run_one(t: dict) -> dict:
            task_id = t["_id"]
            task_title = t["_title"]
            agent_id = t.get("_agent_id", task_id)
            instruction = str(t.get("instruction") or "").strip()
            model_hint = str(t.get("model") or "").strip() or None
            task_type = str(t.get("task_type") or "implement").strip() or "implement"
            await on_orchestrator_event("sub_agent_started", {"task_id": task_id, "title": task_title, "agent_id": agent_id})
            try:
                result = await self._run_sub_agent(
                    idea, branch, output_dir,
                    task_id, task_title, instruction,
                    on_tool_result, on_orchestrator_event,
                    source_root=source_root,
                    prd_content=prd_content,
                    agent_id=agent_id,
                    model_hint=model_hint,
                    task_type=task_type,
                    interface_summary=interface_summary,
                )
                if result.get("success") and max_verify_cycles > 0 and task_type not in ("inspect", "scaffold"):
                    result = await self._run_task_verify_fix_loop(
                        idea, branch, output_dir,
                        t, result,
                        on_tool_result, on_orchestrator_event,
                        prd_content=prd_content,
                        agent_id=agent_id,
                        max_fix_cycles=max_verify_cycles,
                        model_hint=model_hint,
                        interface_summary=interface_summary,
                    )
                # Persist completion immediately so a page refresh shows the correct state
                await on_orchestrator_event("sub_agent_complete", {
                    "task_id": task_id,
                    "title": task_title,
                    "agent_id": agent_id,
                    "summary": result.get("summary", ""),
                    "files_written": result.get("files_written", []),
                    "commands_run": result.get("commands_run", []),
                    "success": result.get("success", True),
                    "blocker": result.get("blocker"),
                })
                return result
            except asyncio.CancelledError:
                if is_user_cancelled and is_user_cancelled(task_id):
                    # Per-task cancel — emit cancelled event and return gracefully
                    # so the orchestrator continues with remaining tasks.
                    await on_orchestrator_event("sub_agent_cancelled", {
                        "task_id": task_id,
                        "title": task_title,
                        "agent_id": agent_id,
                    })
                    return {
                        "summary": "Cancelled by user",
                        "files_written": [],
                        "commands_run": [],
                        "success": False,
                        "blocker": "Cancelled by user",
                    }
                # Full session cancel — notify and re-raise so the session stops.
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
            at = asyncio.create_task(_run_one(tasks[0]))
            if register_task:
                register_task(tasks[0]["_id"], at)
            return [await at]

        limit = self._client._registry.resources.max_parallel_sub_agents
        semaphore = asyncio.Semaphore(max(1, limit))
        logger.debug("sub_agent_batch: running %d task(s) with concurrency limit=%d", len(tasks), limit)

        async def _run_one_limited(t: dict) -> dict:
            async with semaphore:
                return await _run_one(t)

        asyncio_tasks = []
        for t in tasks:
            at = asyncio.create_task(_run_one_limited(t))
            if register_task:
                register_task(t["_id"], at)
            asyncio_tasks.append(at)
        results = await asyncio.gather(*asyncio_tasks, return_exceptions=True)
        out: list[dict] = []
        for t, r in zip(tasks, results):
            if isinstance(r, BaseException):
                if isinstance(r, asyncio.CancelledError):
                    raise r
                logger.error("sub_agent: task '%s' raised: %s", t["_id"], r)
                failure = {
                    "summary": f"Task failed: {r}",
                    "files_written": [],
                    "commands_run": [],
                    "success": False,
                    "blocker": str(r),
                }
                # sub_agent_complete wasn't emitted by _run_one — do it now
                await on_orchestrator_event("sub_agent_complete", {
                    "task_id": t["_id"],
                    "title": t["_title"],
                    "agent_id": t.get("_agent_id"),
                    **failure,
                })
                out.append(failure)
            else:
                out.append(r)
        return out

    async def _run_inspector_agent(
        self,
        output_dir: str,
        paths: list[str],
        focus: str,
        on_orchestrator_event: OnOrchestratorEvent,
    ) -> dict:
        """Read files directly from disk — no LLM call needed. Returns content + stub heuristics."""
        logger.info("inspector: reading %d file(s) directly: %s", len(paths), paths)
        await on_orchestrator_event("orchestrator_tool", {"tool": "inspect_files", "result": {"paths": paths, "focus": focus}})

        _MAX_FILE_CHARS = 1500
        _LANG_MAP = {
            ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript",
            ".jsx": "javascript", ".css": "css", ".html": "html", ".json": "json",
            ".md": "markdown", ".svelte": "svelte", ".vue": "vue", ".go": "go",
            ".rs": "rust", ".rb": "ruby", ".java": "java", ".cs": "csharp",
            ".cpp": "cpp", ".c": "c", ".sh": "bash", ".yaml": "yaml", ".yml": "yaml",
            ".toml": "toml", ".sql": "sql",
        }
        _STUB_MARKERS = (
            "TODO", "FIXME", "raise NotImplementedError", "throw new Error(",
            "# stub", "// stub", "not implemented", "placeholder",
        )

        base = Path(output_dir).resolve() if output_dir else None
        files: list[dict] = []

        for rel_path in paths:
            try:
                if base:
                    full_path = (base / rel_path).resolve()
                    if not str(full_path).startswith(str(base)):
                        files.append({"path": rel_path, "error": "access denied"})
                        continue
                else:
                    full_path = Path(rel_path).resolve()

                raw = full_path.read_bytes()
                if b"\x00" in raw[:512]:
                    files.append({"path": rel_path, "summary": "binary file (skipped)", "has_stubs": False})
                    continue

                text = raw[:_MAX_FILE_CHARS].decode("utf-8", errors="replace")
                truncated = len(raw) > _MAX_FILE_CHARS
                suffix = full_path.suffix.lower()
                language = _LANG_MAP.get(suffix, suffix.lstrip(".") or "text")
                has_stubs = (
                    any(m.lower() in text.lower() for m in _STUB_MARKERS)
                    or _has_semantic_stubs(text, suffix)
                )

                files.append({
                    "path": rel_path,
                    "language": language,
                    "size_bytes": len(raw),
                    "content": text + ("\n...(truncated)" if truncated else ""),
                    "has_stubs": has_stubs,
                    "summary": f"{language}, {len(raw)} bytes" + (" [has TODOs/stubs]" if has_stubs else ""),
                })
            except FileNotFoundError:
                files.append({"path": rel_path, "error": "file not found"})
            except Exception as e:
                files.append({"path": rel_path, "error": str(e)})

        return {"files": files}

    async def _run_task_verification_agent(
        self,
        idea: Idea,
        branch: SolutionBranch,
        output_dir: str,
        task_id: str,
        task_title: str,
        task_instruction: str,
        files_written: list[str],
        on_tool_result: OnToolResult,
        on_orchestrator_event: OnOrchestratorEvent,
        prd_content: str | None = None,
        agent_id: str | None = None,
    ) -> dict:
        """Read the files a sub-agent wrote and verify they are stub-free and correct.

        Returns {verified: bool, issues: [str]}.
        Never blocks on its own failure — returns verified=True on internal error.
        """
        if not files_written:
            return {"verified": True, "issues": []}

        verify_agent_id = f"verify_{agent_id or task_id}"
        await on_orchestrator_event("sub_agent_verify_started", {
            "task_id": task_id,
            "title": f"Verify: {task_title}",
            "agent_id": verify_agent_id,
            "files": files_written,
        })

        # Pre-read each file and verify in 120-line chunks so the model always knows the
        # total file size and can report accurate absolute line numbers.  Small models (3B)
        # routinely skip tool calls and hallucinate issues when asked to call read_file
        # themselves, so embedding content directly is the only reliable approach.
        _CHUNK_SIZE = 120
        _MAX_FILES = 8

        system_prompt = (
            "You are a code verification agent. Review the file chunk below and report problems.\n\n"
            "## What to check\n\n"
            "1. Text-marker stubs: TODO, FIXME, placeholder comments, `raise NotImplementedError`, "
            "or any function body that is ONLY a comment with no code\n"
            "2. Semantic stubs — hollow function bodies. Flag any function/method whose ENTIRE body is:\n"
            "   - `pass` (Python)\n"
            "   - `return None` or bare `return` with no meaningful value\n"
            "   - `return {}` / `return []` / `return ()` when the function is supposed to return data\n"
            "   - `...` (ellipsis) as the sole statement\n"
            "   - An empty block `{}` in TypeScript/JavaScript\n"
            "   These are stubs even when no TODO comment is present.\n"
            "3. Completeness: each function must have real, working logic — not just a skeleton\n\n"
            "## Output format — JSON only, no prose\n\n"
            '{"verified": true, "issues": []}\n'
            "If problems found:\n"
            '{"verified": false, "issues": ["path/file.py: line 42: hollow body — function returns None but must return user data", ...]}\n\n'
            "Use absolute line numbers (1-indexed from the start of the file)."
        )

        all_issues: list[str] = []
        result: dict = {"verified": True, "issues": []}
        try:
            for rel_path in files_written[:_MAX_FILES]:
                abs_path = Path(output_dir) / rel_path
                try:
                    raw_bytes = abs_path.read_bytes()
                    if b"\x00" in raw_bytes[:512]:
                        continue  # skip binary files
                    all_lines = raw_bytes.decode("utf-8", errors="replace").splitlines()
                except Exception as _e:
                    logger.warning("verification_agent: could not read %s: %s", rel_path, _e)
                    continue

                total_lines = len(all_lines)
                if not total_lines:
                    continue

                for chunk_start in range(0, total_lines, _CHUNK_SIZE):
                    chunk_end = min(chunk_start + _CHUNK_SIZE, total_lines)
                    chunk_content = "\n".join(all_lines[chunk_start:chunk_end])
                    is_last = chunk_end >= total_lines
                    chunk_note = (
                        f"lines {chunk_start + 1}–{chunk_end} of {total_lines} (final chunk)"
                        if is_last
                        else f"lines {chunk_start + 1}–{chunk_end} of {total_lines} — more chunks follow"
                    )
                    user_prompt = (
                        f"Task: {task_instruction}\n\n"
                        f"## {rel_path} ({chunk_note})\n\n"
                        f"```\n{chunk_content}\n```\n\n"
                        "Report stub markers and incomplete logic in this chunk. "
                        "Use absolute line numbers. Return JSON only."
                    )
                    async with AsyncSessionLocal() as sub_db:
                        raw = await self._client.call(
                            stage_key="phase3_task_verify",
                            messages=[
                                Message(role="system", content=system_prompt),
                                Message(role="user", content=user_prompt),
                            ],
                            session=sub_db,
                            idea_id=idea.id,
                            branch_id=branch.id,
                            call_index=0,
                        )
                    if isinstance(raw, dict):
                        all_issues.extend(raw.get("issues") or [])

            result = {
                "verified": len(all_issues) == 0,
                "issues": all_issues,
            }
        except Exception as exc:
            logger.warning("verification_agent: task '%s' failed — skipping: %s", task_title, exc)

        await on_orchestrator_event("sub_agent_verify_complete", {
            "task_id": task_id,
            "title": f"Verify: {task_title}",
            "agent_id": verify_agent_id,
            "verified": result["verified"],
            "issues": result["issues"],
        })
        logger.info(
            "verification_agent: task '%s' verified=%s issues=%d",
            task_title, result["verified"], len(result["issues"]),
        )
        for _issue in result["issues"]:
            logger.info("verification_agent:   issue: %s", _issue)
        return result

    async def _run_task_verify_fix_loop(
        self,
        idea: Idea,
        branch: SolutionBranch,
        output_dir: str,
        task: dict,
        impl_result: dict,
        on_tool_result: OnToolResult,
        on_orchestrator_event: OnOrchestratorEvent,
        prd_content: str | None = None,
        agent_id: str | None = None,
        max_fix_cycles: int = 3,
        model_hint: str | None = None,
        interface_summary: str | None = None,
    ) -> dict:
        """Verify the implementation, then fix+re-verify until passing or max_fix_cycles reached."""
        task_id = task["_id"]
        task_title = task["_title"]
        instruction = str(task.get("instruction") or "")
        files_written = list(impl_result.get("files_written") or [])

        for cycle in range(max_fix_cycles + 1):
            verify = await self._run_task_verification_agent(
                idea, branch, output_dir,
                task_id, task_title, instruction,
                files_written,
                on_tool_result, on_orchestrator_event,
                prd_content=prd_content,
                agent_id=agent_id,
            )
            if verify["verified"]:
                break

            # Discard hallucinated issues — two checks:
            # 1. Claimed line number exceeds actual file length.
            # 2. Issue claims a structural problem (hollow body, stub, returns None) but the
            #    actual line is an import, decorator, comment, or blank — clearly not a function body.
            _STRUCTURAL_KEYWORDS = ("hollow body", "stub", "returns none", "not implemented", "pass only", "ellipsis")
            _NON_BODY_PREFIXES = ("import ", "from ", "@", "#", '"""', "'''")
            real_issues = []
            for issue in verify["issues"]:
                _line_match = re.search(r"\bline[s]?\s+(\d+)", issue, re.IGNORECASE)
                if _line_match:
                    _claimed_line = int(_line_match.group(1))
                    _issue_file = issue.split(":")[0].strip()
                    _abs = Path(output_dir) / _issue_file
                    if _abs.exists():
                        try:
                            _file_lines = _abs.read_text(encoding="utf-8", errors="replace").splitlines()
                            _actual_lines = len(_file_lines)
                            if _claimed_line > _actual_lines:
                                logger.warning(
                                    "verify_fix_loop: dropping hallucinated issue (line %d > file length %d): %s",
                                    _claimed_line, _actual_lines, issue,
                                )
                                continue
                            # Check if line content is incompatible with the reported issue type
                            _issue_lower = issue.lower()
                            if any(kw in _issue_lower for kw in _STRUCTURAL_KEYWORDS):
                                _line_content = _file_lines[_claimed_line - 1].strip()
                                if not _line_content or any(_line_content.startswith(p) for p in _NON_BODY_PREFIXES):
                                    logger.warning(
                                        "verify_fix_loop: dropping hallucinated issue (line %d is '%s', not a function body): %s",
                                        _claimed_line, _line_content[:40], issue,
                                    )
                                    continue
                        except OSError:
                            pass
                real_issues.append(issue)

            if not real_issues:
                logger.info(
                    "verify_fix_loop: all %d reported issue(s) were hallucinated (invalid line numbers) — treating as verified",
                    len(verify["issues"]),
                )
                break

            verify["issues"] = real_issues

            if cycle >= max_fix_cycles:
                logger.warning(
                    "verify_fix_loop: task '%s' failed verification after %d fix cycle(s)",
                    task_title, max_fix_cycles,
                )
                impl_result["success"] = False
                impl_result["blocker"] = (
                    f"Failed verification after {max_fix_cycles} fix attempt(s). "
                    "Issues: " + "; ".join(verify["issues"][:5])
                )
                break

            issues_text = "\n".join(f"- {i}" for i in verify["issues"])
            fix_instruction = (
                f"Original task:\n{instruction}\n\n"
                f"Files that need fixing:\n" + "\n".join(f"- {f}" for f in files_written) + "\n\n"
                f"Issues found by verification (fix cycle {cycle + 1}):\n{issues_text}\n\n"
                "Read each file listed above, fix every issue, and write the corrected file back to the SAME path. "
                "Do NOT create any new files or rename existing ones — only overwrite the exact paths listed above. "
                "Do NOT introduce new TODOs, stubs, or placeholders — write complete working code."
            )
            fix_id = f"{task_id}_fix{cycle + 1}"
            fix_title = f"Fix ({cycle + 1}): {task_title}"
            fix_agent_id = f"fix_{agent_id or task_id}_{cycle + 1}"

            await on_orchestrator_event("sub_agent_fix_started", {
                "task_id": fix_id,
                "title": fix_title,
                "agent_id": fix_agent_id,
                "parent_task_id": task_id,
                "cycle": cycle + 1,
            })
            fix_result = await self._run_sub_agent(
                idea, branch, output_dir,
                fix_id, fix_title, fix_instruction,
                on_tool_result, on_orchestrator_event,
                source_root=None,
                prd_content=prd_content,
                agent_id=fix_agent_id,
                model_hint=model_hint,
                interface_summary=interface_summary,
            )
            await on_orchestrator_event("sub_agent_fix_complete", {
                "task_id": fix_id,
                "title": fix_title,
                "agent_id": fix_agent_id,
                "parent_task_id": task_id,
                "cycle": cycle + 1,
                "success": fix_result.get("success", False),
                "summary": fix_result.get("summary", ""),
                "files_written": fix_result.get("files_written", []),
                "commands_run": fix_result.get("commands_run", []),
                "blocker": fix_result.get("blocker"),
            })
            for f in fix_result.get("files_written") or []:
                if f not in files_written:
                    files_written.append(f)

        impl_result["files_written"] = files_written
        return impl_result

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
        source_root: str | None = None,
        prd_content: str | None = None,
        agent_id: str | None = None,
        model_hint: str | None = None,
        task_type: str = "implement",
        interface_summary: str | None = None,
    ) -> dict:
        stage_cfg = self._client._registry.get_stage("phase3_sub_agent")

        _files_edited: list[str] = []  # reset each attempt to catch fabricated results
        _tool_counts: dict[str, int] = {}  # reset each attempt; logged at task outcome

        async def _wrapped_on_tool(tool_name: str, result: dict) -> None:
            _tool_counts[tool_name] = _tool_counts.get(tool_name, 0) + 1
            await on_tool_result(tool_name, result)
            if tool_name == "file_edit":
                _files_edited.append(result.get("path", ""))
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

        async def _on_text_response(raw: str) -> None:
            text = raw.strip()
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    text = str(obj.get("message") or obj.get("summary") or text).strip()
            except Exception:
                pass
            if text:
                await on_orchestrator_event("sub_agent_update", {
                    "task_id": task_id,
                    "update_type": "message",
                    "detail": text[:300],
                })

        async def _handle_read_prd(_args: dict) -> dict:
            from app.tools.context_reducer import reduce_prd
            reduced = reduce_prd(prd_content or "", task_instruction)
            return {"prd": reduced, "length": len(reduced)}

        last_result: dict = {}
        prior_failures: list[dict] = []

        import time as _time
        from app import telemetry as _telemetry
        from app.tools.shell_runner import background_process_manager as _bg_procs

        # Rank models by telemetry (success_rate DESC, avg_ms ASC); YAML order until enough data.
        # Locked here — before any attempt — so failures within this task don't shift the order.
        _candidate_models = [m.model for m in stage_cfg.selectable_models]
        _ranked_names = _telemetry.rank_models("phase3_sub_agent", _candidate_models, project_id=str(idea.id))
        _by_model = {m.model: m for m in stage_cfg.selectable_models}
        # model_hint may be a literal model tag override (e.g. from a manual retry)
        if model_hint and model_hint in _by_model:
            _ranked_names = [model_hint] + [m for m in _ranked_names if m != model_hint]
        models_to_try = [_by_model[n] for n in _ranked_names if n in _by_model]
        if not models_to_try:
            models_to_try = list(stage_cfg.selectable_models)
        logger.info(
            "sub_agent: starting task '%s' (%s) agent=%s ranked_models=%s",
            task_title, task_id, agent_id or "?", [m.model for m in models_to_try],
        )

        # Pre-fetch relevant memory observations once — injected into every attempt's
        # prompt so agents don't need to call memory_search themselves.
        _memory_context: str | None = None
        if task_type != "scaffold":
            import app.memory as _mem
            _mem_results = await _mem.search(str(idea.id), task_instruction, top_k=5)
            if _mem_results:
                _memory_context = "\n".join(
                    f"{r['file_path']} — {r['observation'].strip()}"
                    for r in _mem_results
                )

        try:
          for attempt, model_sm in enumerate(models_to_try):
            _files_edited.clear()
            _tool_counts.clear()
            # Kill any background processes left behind by the previous attempt
            # (e.g. a dev server started for build-checking that was never stopped).
            if attempt > 0:
                _bg_procs.cleanup_dir(output_dir)
            _telemetry.set_call_context(
                is_fallback=attempt > 0,
                fallback_from=models_to_try[attempt - 1].model if attempt > 0 else None,
                model_type=model_sm.model,
                task_id=task_id,
            )
            if attempt > 0:
                logger.info("sub_agent: task '%s' — fallback attempt %d with model %s", task_title, attempt, model_sm.model)
                await on_orchestrator_event("sub_agent_model_fallback", {
                    "task_id": task_id,
                    "model": model_sm.model,
                    "attempt": attempt,
                })

            user_prompt = _sub_agent_user_prompt(
                task_instruction, output_dir, idea.name,
                prior_failures=prior_failures,
                source_root=source_root,
                task_type=task_type,
                interface_summary=interface_summary,
                memory_context=_memory_context,
            )

            # Skip this model if the prompt is too large for its context window.
            effective_num_ctx = model_sm.num_ctx or stage_cfg.num_ctx
            sys_prompt = _sub_agent_system_prompt(task_type=task_type)
            est_tokens = (len(sys_prompt) + len(user_prompt)) // 3
            if est_tokens > int(effective_num_ctx * 0.85):
                logger.warning(
                    "sub_agent: task '%s' — skipping model %s (est %d tokens > %d ctx limit)",
                    task_title, model_sm.model, est_tokens, effective_num_ctx,
                )
                continue

            _attempt_start = _time.monotonic()
            _telemetry.suppress_next_call()  # orchestrator logs task-level outcome below

            # Token-level streaming from Ollama — shows sub-agent activity in the task card.
            # Called from the async event loop so we use create_task for fire-and-forget.
            _sub_stream_in_think = False

            def _sub_on_token(token: str) -> None:
                nonlocal _sub_stream_in_think
                was_in_think = _sub_stream_in_think
                _sub_stream_in_think, think_content = _extract_think_content(token, _sub_stream_in_think)
                # For thinking models (<think> tags): use extracted think content.
                # For non-thinking models: stream the raw token when outside any
                # think block — they produce reasoning text directly before tool calls.
                if think_content:
                    visible = think_content
                elif not was_in_think and not _sub_stream_in_think:
                    # Outside any think block — raw text from a non-thinking model
                    visible = token
                else:
                    return  # inside a think block but not its text content — skip
                stripped = visible.strip()
                # Filter structural JSON/XML tokens (tool-call JSON, final response JSON)
                if not stripped or stripped[0] in ('{', '}', '[', ']', '<', '"', ':'):
                    return
                asyncio.create_task(
                    on_orchestrator_event("sub_agent_token", {
                        "task_id": task_id,
                        "content": visible,
                    })
                )

            try:
                async with AsyncSessionLocal() as sub_db:
                    last_result = await self._client.call_with_tools(
                        stage_key="phase3_sub_agent",
                        messages=[
                            Message(role="system", content=sys_prompt),
                            Message(role="user", content=user_prompt),
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
                        on_text_response=_on_text_response,
                        model_override=model_sm.model,
                        num_ctx_override=model_sm.num_ctx,
                        timeout_override=model_sm.timeout_seconds,
                        think_override=model_sm.think,
                        extra_tools=_sub_agent_extra_tools(),
                        custom_tool_handlers={
                            "read_prd": _handle_read_prd,
                            **_memory_handlers(idea.id),
                        },
                        agent_id=agent_id,
                        on_token=_sub_on_token,
                        json_schema=(
                            '{"summary": "what was done", "files_written": ["path/to/file"], '
                            '"commands_run": ["npm install"], "success": true, "blocker": null}'
                        ),
                    )
                    last_result = _normalize_sub_agent_result(last_result, task_title, task_type=task_type, actual_shell_calls=_tool_counts.get("run_shell", 0))

                    # ----------------------------------------------------------
                    # Nudge pass — give the model one chance to self-correct
                    # before hard-failing.  Covers two common small-model failure
                    # modes:
                    #   1. Claimed files written but never called file_edit
                    #   2. Reported success with no files and no commands
                    # We send the bad response back as an assistant turn so the
                    # model sees what it said, then add a corrective user message.
                    # ----------------------------------------------------------
                    _nudge_msg: str | None = None
                    _claimed_before_nudge: list[str] = []

                    if last_result.get("success") and task_type != "scaffold":
                        _claimed_before_nudge = [f for f in (last_result.get("files_written") or []) if f]
                        if _claimed_before_nudge and not _files_edited:
                            _files_list = ", ".join(_claimed_before_nudge[:3])
                            if len(_claimed_before_nudge) > 3:
                                _files_list += f" (and {len(_claimed_before_nudge) - 3} more)"
                            _nudge_msg = (
                                f"CORRECTION: You returned success and claimed to have written "
                                f"{_files_list}, but `file_edit` was never called — "
                                "those files do not exist on disk. "
                                "You MUST call `file_edit` for each file to actually create it. "
                                "Describing a file in text does NOT write it. "
                                "Call `file_edit` now, then return the JSON summary."
                            )
                    elif not last_result.get("success") and "produced no files or commands" in (last_result.get("blocker") or ""):
                        _nudge_msg = (
                            "CORRECTION: You returned success but wrote no files and ran no commands — "
                            "nothing was done. You must call `file_edit` to write the required files. "
                            "Return the JSON summary only AFTER all files are written."
                        )

                    if _nudge_msg:
                        logger.warning(
                            "sub_agent: task '%s' attempt %d — nudging instead of failing: %s",
                            task_title, attempt, _nudge_msg[:100],
                        )
                        _files_edited.clear()
                        _tool_counts.clear()
                        _telemetry.suppress_next_call()
                        _prev_json = json.dumps(last_result, ensure_ascii=False)
                        async with AsyncSessionLocal() as nudge_db:
                            nudge_raw = await self._client.call_with_tools(
                                stage_key="phase3_sub_agent",
                                messages=[
                                    Message(role="system", content=sys_prompt),
                                    Message(role="user", content=user_prompt),
                                    Message(role="assistant", content=_prev_json),
                                    Message(role="user", content=_nudge_msg),
                                ],
                                session=nudge_db,
                                idea_id=idea.id,
                                branch_id=branch.id,
                                allowed_file_dir=output_dir,
                                explore_only=False,
                                max_tool_rounds=60,
                                return_json=True,
                                call_index=0,
                                on_tool_result=_wrapped_on_tool,
                                on_text_response=_on_text_response,
                                model_override=model_sm.model,
                                num_ctx_override=model_sm.num_ctx,
                                timeout_override=model_sm.timeout_seconds,
                                think_override=model_sm.think,
                                extra_tools=_sub_agent_extra_tools(),
                                custom_tool_handlers={
                                    "read_prd": _handle_read_prd,
                                    **_memory_handlers(idea.id),
                                },
                                agent_id=agent_id,
                                on_token=_sub_on_token,
                                json_schema=(
                                    '{"summary": "what was done", "files_written": ["path/to/file"], '
                                    '"commands_run": ["npm install"], "success": true, "blocker": null}'
                                ),
                            )
                            last_result = _normalize_sub_agent_result(nudge_raw, task_title, task_type=task_type, actual_shell_calls=_tool_counts.get("run_shell", 0))

                    # ----------------------------------------------------------
                    # Hard verification — runs on the final result (post-nudge
                    # or original).  Models that ignored the nudge fail here.
                    # ----------------------------------------------------------
                    if last_result.get("success"):
                        claimed = [f for f in (last_result.get("files_written") or []) if f]
                        if claimed and not _files_edited and task_type != "scaffold":
                            # Scaffold tasks legitimately create files via CLI commands (dotnet new,
                            # npm init, cargo new, etc.) without calling file_edit — check disk instead.
                            last_result["success"] = False
                            last_result["blocker"] = (
                                f"Model claimed {len(claimed)} file(s) written but never called "
                                "file_edit — result is fabricated. Must use file_edit to write files."
                            )
                            logger.warning(
                                "sub_agent: task '%s' attempt %d — fabricated result: claimed %s but no file_edit",
                                task_title, attempt, claimed,
                            )
                        elif claimed:
                            missing = [f for f in claimed if not (Path(output_dir) / f).exists()]
                            if missing:
                                last_result["success"] = False
                                last_result["blocker"] = (
                                    f"Files claimed but not found on disk: {', '.join(missing[:5])}"
                                )
                                logger.warning(
                                    "sub_agent: task '%s' attempt %d — claimed files missing on disk: %s",
                                    task_title, attempt, missing,
                                )
                            else:
                                # Detect fake binary assets: audio/image files written as text stubs
                                # (real audio is typically >10 KB; real images >1 KB)
                                _AUDIO_EXTS = frozenset({".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".opus"})
                                _IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
                                _MIN_AUDIO_BYTES = 10_000
                                _MIN_IMAGE_BYTES = 1_000
                                fake_assets = []
                                for f in claimed:
                                    p = Path(output_dir) / f
                                    ext = p.suffix.lower()
                                    if ext in _AUDIO_EXTS and p.stat().st_size < _MIN_AUDIO_BYTES:
                                        fake_assets.append(f)
                                    elif ext in _IMAGE_EXTS and p.stat().st_size < _MIN_IMAGE_BYTES:
                                        fake_assets.append(f)
                                if fake_assets:
                                    # Remove the stub files so they don't pollute the project
                                    for f in fake_assets:
                                        try:
                                            (Path(output_dir) / f).unlink()
                                        except OSError:
                                            pass
                                    last_result["success"] = False
                                    last_result["blocker"] = (
                                        f"Fake asset stubs detected and removed: {', '.join(fake_assets[:5])}. "
                                        "Use generate_audio_music / generate_audio_sfx / generate_audio_speech "
                                        "for audio, or generate_image for images."
                                    )
                                    logger.warning(
                                        "sub_agent: task '%s' attempt %d — fake asset stubs: %s",
                                        task_title, attempt, fake_assets,
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

            # Log task-level outcome — success means the sub-agent actually completed
            # its task, not just that the inference call returned without an exception.
            # Clear the suppress flag first: if call_with_tools threw before its own
            # log_call, the flag was never consumed and would silently drop this record.
            _telemetry.clear_suppress()
            _telemetry.set_tool_counts(_tool_counts)
            _sub_tokens_prompt, _sub_tokens_completion = _telemetry.get_last_call_tokens()
            _telemetry.log_call(
                stage="phase3_sub_agent",
                model=model_sm.model,
                backend=stage_cfg.backend,
                duration_ms=int((_time.monotonic() - _attempt_start) * 1000),
                success=bool(last_result.get("success", True)) and not last_result.get("blocker"),
                error=last_result.get("blocker") or None,
                tokens_prompt=_sub_tokens_prompt,
                tokens_completion=_sub_tokens_completion,
            )

            if last_result.get("success", True):
                return last_result

            # Fast-fail: if the backend server itself is unreachable, every
            # remaining fallback will fail for the same reason — skip them all
            # so we don't pollute telemetry with false model failures.
            _blocker_lower = (last_result.get("blocker") or "").lower()
            _CONN_ERRORS = (
                "all connection attempts failed",
                "connection refused",
                "cannot connect to host",
                "clientconnectorerror",
                "serverdisconnectederror",
                "no route to host",
                "name or service not known",
            )
            if any(kw in _blocker_lower for kw in _CONN_ERRORS):
                _remaining = len(models_to_try) - attempt - 1
                logger.error(
                    "sub_agent: task '%s' — backend '%s' unreachable, skipping %d fallback(s)",
                    task_title, stage_cfg.backend, _remaining,
                )
                break

            handoff = await self._generate_retry_handoff(task_instruction, last_result, attempt + 1)
            prior_failures.append({
                "attempt": attempt + 1,
                "blocker": last_result.get("blocker") or "",
                "summary": last_result.get("summary") or "",
                "handoff": handoff,
                "files_written": list(last_result.get("files_written") or []),
            })
            _is_last_model = attempt + 1 >= len(models_to_try)
            logger.info("sub_agent: task '%s' attempt %d unsuccessful — blocker=%r summary=%r — %s",
                        task_title, attempt,
                        last_result.get("blocker") or "",
                        (last_result.get("summary") or "")[:120],
                        "no more fallbacks" if _is_last_model else f"retrying with {models_to_try[attempt + 1].model}")
            if _is_last_model:
                last_result = {**last_result, "permanently_failed": True}

          return last_result
        finally:
            # Always clean up any background processes the sub-agent left running
            # (dev servers, watchers, etc.) so they don't hold ports across tasks.
            _bg_procs.cleanup_dir(output_dir)

    async def _generate_retry_handoff(
        self,
        task_instruction: str,
        failed_result: dict,
        attempt: int,
    ) -> str:
        """
        Call context_summarizer to produce a structured handoff note for the retry agent.
        Returns a plain-text note on success, falls back to raw summary/blocker on any error.
        """
        from app.inference.base import InferenceRequest, Message
        from app import telemetry as _telemetry

        files_written = failed_result.get("files_written") or []
        commands_run = failed_result.get("commands_run") or []
        summary = (failed_result.get("summary") or "").strip()
        blocker = (failed_result.get("blocker") or "").strip()

        files_str = "\n".join(f"  - {f}" for f in files_written) if files_written else "  (none)"
        cmds_str = "\n".join(f"  - {c}" for c in commands_run) if commands_run else "  (none)"

        user_content = (
            f"An agent (attempt {attempt}) was given this task:\n{task_instruction}\n\n"
            f"Files it wrote to disk:\n{files_str}\n\n"
            f"Commands it ran:\n{cmds_str}\n\n"
            f"Its final report: {summary or '(none)'}\n"
            f"It stopped because: {blocker or '(no explicit blocker reported)'}\n\n"
            "Write a handoff note for the retry agent using exactly this format:\n"
            "GOAL: <one sentence — what the task must accomplish>\n"
            "DONE: <what was written and is working, or 'nothing'>\n"
            "NOT DONE: <what still needs to be implemented>\n"
            "FAILED BECAUSE: <the specific technical reason>\n"
            "START FROM: <the exact file or step the retry should begin at>"
        )

        _model = "unknown"
        _backend = "unknown"
        try:
            stage_cfg = self._client._registry.get_stage("context_summarizer")
            _model = stage_cfg.model
            _backend = stage_cfg.backend
            driver = self._client._get_driver(stage_cfg.backend)
            req = InferenceRequest(
                model=stage_cfg.model,
                messages=[
                    Message(role="system", content=(
                        "You write concise agent handoff notes. "
                        "Output only the note in the requested format, no preamble."
                    )),
                    Message(role="user", content=user_content),
                ],
                format="",
                temperature=stage_cfg.temperature,
                max_tokens=stage_cfg.max_tokens,
                num_ctx=stage_cfg.num_ctx,
                timeout_seconds=stage_cfg.timeout_seconds,
            )
            import time as _time
            _t = _time.monotonic()
            resp = await driver.complete(req)
            _telemetry.log_call(
                stage="context_summarizer",
                model=stage_cfg.model,
                backend=stage_cfg.backend,
                duration_ms=int((_time.monotonic() - _t) * 1000),
                success=True,
                tokens_prompt=resp.tokens_prompt,
                tokens_completion=resp.tokens_completion,
                _ctx={},
            )
            return (resp.content or "").strip()
        except Exception as exc:
            logger.debug("retry_handoff: summarizer failed (%s) — using raw failure info", exc)
            _telemetry.log_call(
                stage="context_summarizer", model=_model, backend=_backend,
                duration_ms=None, success=False, error=str(exc), _ctx={},
            )
            # Fallback: construct a minimal handoff from raw fields
            parts = []
            if summary:
                parts.append(f"DONE: {summary[:300]}")
            if blocker:
                parts.append(f"FAILED BECAUSE: {blocker[:300]}")
            return "\n".join(parts) if parts else "No handoff information available."
