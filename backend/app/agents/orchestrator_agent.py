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
                    "Override the auto-detected build command with a single command "
                    "(e.g. 'npm run build', 'npm run typecheck', 'cargo check'). "
                    "Do NOT use && chaining — pass one command only. "
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
        "List tasks in the hierarchical implementation plan (Area → Story → Task). "
        "Each entry shows status, depth, and child_count so you know whether to drill deeper. "
        "Use parent_id to zoom into a specific area or story and see only its sub-tasks. "
        "Supports pagination — use offset/limit on large plans."
    ),
    parameters={
        "type": "object",
        "properties": {
            "offset":    {"type": "integer", "description": "Number of items to skip (default 0)"},
            "limit":     {"type": "integer", "description": "Max items to return (default 20, max 50)"},
            "parent_id": {"type": "string",  "description": "If set, list only the direct children of this area/story id"},
        },
        "required": [],
    },
)

_PLAN_ADD_TOOL = ToolDefinition(
    name="plan_add",
    description=(
        "Add a new pending task to the plan. "
        "Use parent_id to nest it under any existing area, story, or task — supports arbitrary depth. "
        "Each leaf task must be scoped to exactly 1 file. "
        "To add a new area/grouping node (no instruction), omit instruction and it will be treated as a container."
    ),
    parameters={
        "type": "object",
        "properties": {
            "id":          {"type": "string", "description": "Unique snake_case identifier — must not already exist in the plan"},
            "title":       {"type": "string", "description": "Short title ≤60 chars"},
            "instruction": {"type": "string", "description": "Complete self-contained task instruction (required for leaf tasks)"},
            "parent_id":   {"type": "string", "description": "Id of the parent node to nest under — use plan_list() to find available ids"},
            "notes":       {"type": "string", "description": "Optional notes"},
        },
        "required": ["id", "title"],
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
        # Split &&-chained commands and run sequentially (PowerShell 5.1 doesn't support &&).
        parts = [p.strip() for p in command.split("&&") if p.strip()]
        all_out: list[str] = []
        for part in parts:
            result = await run_shell_command(part, output_dir, timeout_seconds=180)
            combined = (result.stdout + "\n" + result.stderr).strip()
            all_out.append(f"$ {part}\n{combined}".rstrip())
            if result.exit_code != 0:
                full = "\n\n".join(all_out)
                if len(full) > 5000:
                    full = "…(truncated)\n" + full[-5000:]
                return {
                    "success": False,
                    "exit_code": result.exit_code,
                    "command": command,
                    "output": full,
                    "timed_out": result.timed_out,
                }
        full = "\n\n".join(all_out)
        if len(full) > 5000:
            full = "…(truncated)\n" + full[-5000:]
        return {"success": True, "exit_code": 0, "command": command, "output": full, "timed_out": False}

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
        "## Plan management — your persistent hierarchical task list\n\n"
        "The plan is pre-built and stored on disk in a nested Area → Story → Task hierarchy "
        "(like Agile Epics/Features/Tasks). It survives restarts. Your job is to EXECUTE the plan, "
        "not rebuild it from scratch.\n\n"
        "**Workflow:**\n"
        "1. **Round 1**: call `plan_list()` — the plan is already populated. "
        "If plan_list returns no tasks, THEN build the plan by calling `plan_add` for every "
        "PRD feature (scoped to exactly 1 file each).\n"
        "2. **Each round**: call `plan_next()` to get the next pending leaf task. "
        "It returns the task with its `context` breadcrumb (e.g. 'Backend API > WebSocket Service'). "
        "Call `plan_update(id, status='in_progress')` before dispatching. "
        "Use its `instruction` field verbatim as the task instruction in `next_tasks`. "
        "**CRITICAL**: copy the EXACT `id` from the plan task into the `next_tasks` entry — "
        "the runtime matches dispatched tasks to plan tasks by id. Wrong id = plan never advances.\n"
        "   - If `plan_next` returns `retry=true`: read `notes`, use a different approach.\n"
        "   - If a returned task is clearly obsolete (file already exists, superseded by another task): "
        "call `plan_update(id, status='skipped')` and call `plan_next()` again.\n"
        "3. **After task success**: call `plan_update(id, status='done')`. "
        "Completion propagates upward — parent stories/areas auto-complete when all children are done.\n"
        "4. **After permanent failure**: call `plan_remove(id)`, then `plan_add` 1-2 smaller "
        "replacement tasks each touching exactly 1 file. Use `parent_id` to add under the same story.\n"
        "5. **When plan_next returns null**: all tasks complete — set done=true.\n\n"
        "- `plan_list(offset?, limit?, parent_id?)` — nested view of plan; each entry shows `child_count` so you know whether to drill deeper. Pass `parent_id` to zoom into a specific area or story.\n"
        "- `plan_add(id, title, instruction, parent_id?)` — add a leaf task (optionally under a parent)\n"
        "- `plan_update(id, status?, title?, instruction?, notes?)` — update; propagates completion up\n"
        "- `plan_remove(id)` — remove task and its children\n"
        "- `plan_next()` — get the next pending leaf task with context breadcrumb\n\n"
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


def _iter_all_plan_tasks(nodes: list):
    """Depth-first generator over every node in a nested plan tree."""
    for n in nodes:
        yield n
        yield from _iter_all_plan_tasks(n.get("children") or [])


def _find_plan_category(output_dir: str, task_id: str) -> str | None:
    """Return the top-level plan group title that contains task_id, or None."""
    import json as _j
    try:
        pf = Path(output_dir) / ".think-plan.json"
        if not pf.exists():
            return None
        for top in _j.loads(pf.read_text(encoding="utf-8")).get("tasks", []):
            for t in _iter_all_plan_tasks([top]):
                if t.get("id") == task_id:
                    return top.get("title")
    except Exception:
        pass
    return None


def _auto_complete_plan_parents(nodes: list) -> bool:
    """Bottom-up: mark a parent done when all its children are terminal. Returns True if anything changed."""
    dirty = False
    for n in nodes:
        ch = n.get("children") or []
        if ch:
            if _auto_complete_plan_parents(ch):
                dirty = True
            if all(c.get("status") in ("done", "skipped") for c in ch):
                if n.get("status") not in ("done", "skipped"):
                    n["status"] = "done"
                    dirty = True
    return dirty


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
    dispatch_errors: list[str] | None = None,
) -> str:
    # Runtime dispatch errors — shown BEFORE task history so model sees them immediately.
    # These are NOT tasks and must NOT be treated as plan entries.
    dispatch_error_block = ""
    if dispatch_errors:
        shown = dispatch_errors[-8:]  # last 8 to keep context tight
        lines = "\n".join(f"- {e}" for e in shown)
        dispatch_error_block = (
            "\n\n## ⚠ Runtime Dispatch Errors\n\n"
            "The following are **runtime error messages**, NOT completed tasks.\n"
            "Do NOT call plan_remove on these. Do NOT treat them as task IDs in the plan.\n\n"
            f"{lines}"
        )

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

                def _count_pending_recursive(nodes: list) -> int:
                    c = 0
                    for _n in nodes:
                        _ch = _n.get("children") or []
                        if _ch:
                            c += _count_pending_recursive(_ch)
                        elif _n.get("status") in ("pending", "in_progress", "failed"):
                            c += 1
                    return c

                _plan_pending_count = _count_pending_recursive(_plan_tasks)
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
        if not _plan_is_empty:
            start_hint = (
                "## Milestone 0: Orient — review the PRD and pre-built plan before any work\n\n"
                "The PRD is in your system prompt above. The implementation plan has already been built for you.\n\n"
                "DO NOT call `list_files` or `inspect_files` — there is nothing on disk yet.\n"
                "DO NOT dispatch any tasks yet.\n\n"
                "Your ONLY actions this round:\n"
                "1. Re-read the PRD in your system prompt — note the tech stack, entry points, and key features.\n"
                "2. Call `plan_list()` to see the top-level areas.\n"
                "3. Call `plan_list(parent_id=<area_id>)` for each area to review its stories and tasks.\n"
                "4. Call `plan_next()` — it returns the first pending leaf task with its full instruction.\n"
                "5. Call `plan_update(id, status='in_progress')` and dispatch that task.\n\n"
                "The plan already includes setup/scaffold tasks — trust it and follow it in order."
            )
        else:
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
        f"{dispatch_error_block}"
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
    """Return custom tool handlers for the five plan tools, backed by .think-plan.json.

    The plan file uses a nested hierarchy (Area → Story → Task) mirroring Agile
    Epics/Features/Tasks.  Each node has:
      id, title, status, children (list, may be empty for leaves), instruction (leaves only), notes.
    Completion propagates upward: when all children are done/skipped, the parent auto-completes.
    Old flat plans (no children key) remain fully compatible — flat tasks are treated as leaves.
    """
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

    # ── Tree traversal helpers ─────────────────────────────────────────────

    def _is_leaf(t: dict) -> bool:
        return not (t.get("children") or [])

    def _find_by_id(nodes: list, target_id: str) -> dict | None:
        for n in nodes:
            if n.get("id") == target_id:
                return n
            found = _find_by_id(n.get("children") or [], target_id)
            if found:
                return found
        return None

    def _find_path_to(nodes: list, target_id: str, path: list) -> list | None:
        for n in nodes:
            current = path + [n]
            if n.get("id") == target_id:
                return current
            result = _find_path_to(n.get("children") or [], target_id, current)
            if result:
                return result
        return None

    def _count_pending_leaves(nodes: list) -> int:
        count = 0
        for n in nodes:
            children = n.get("children") or []
            if children:
                count += _count_pending_leaves(children)
            elif n.get("status") in ("pending", "failed", "in_progress"):
                count += 1
        return count

    def _next_leaf(nodes: list) -> dict | None:
        """DFS: in_progress first, then pending/failed — returns first actionable leaf."""
        # Pass 1: in_progress (crash recovery)
        for n in nodes:
            if n.get("status") == "in_progress" and _is_leaf(n):
                return n
            found = _next_leaf_pass1(n.get("children") or [])
            if found:
                return found
        # Pass 2: first pending or failed leaf
        return _next_leaf_pass2(nodes)

    def _next_leaf_pass1(nodes: list) -> dict | None:
        for n in nodes:
            if n.get("status") == "in_progress" and _is_leaf(n):
                return n
            found = _next_leaf_pass1(n.get("children") or [])
            if found:
                return found
        return None

    def _next_leaf_pass2(nodes: list) -> dict | None:
        for n in nodes:
            if n.get("status") in ("done", "skipped"):
                continue
            children = n.get("children") or []
            if children:
                found = _next_leaf_pass2(children)
                if found:
                    return found
            elif n.get("status") in ("pending", "failed"):
                return n
        return None

    def _auto_complete_ancestors(root_tasks: list, completed_id: str) -> bool:
        """Walk ancestors of completed_id and mark them done if all children are terminal."""
        path = _find_path_to(root_tasks, completed_id, [])
        if not path or len(path) < 2:
            return False
        dirty = False
        # Walk from immediate parent toward root
        for ancestor in reversed(path[:-1]):
            children = ancestor.get("children") or []
            if children and all(c.get("status") in ("done", "skipped") for c in children):
                if ancestor.get("status") not in ("done", "skipped"):
                    ancestor["status"] = "done"
                    dirty = True
            else:
                break
        return dirty

    def _remove_by_id(nodes: list, target_id: str) -> tuple[list, bool]:
        result, found = [], False
        for n in nodes:
            if n.get("id") == target_id:
                found = True
                continue
            children = n.get("children") or []
            if children:
                new_children, child_found = _remove_by_id(children, target_id)
                n = dict(n)
                n["children"] = new_children
                found = found or child_found
            result.append(n)
        return result, found

    def _flatten_for_display(nodes: list, depth: int = 0) -> list[dict]:
        """Return a flat list with depth and child_count so the orchestrator can navigate the tree."""
        result = []
        for n in nodes:
            children = n.get("children") or []
            entry: dict = {
                "id": n["id"],
                "title": n.get("title", ""),
                "status": n.get("status", "pending"),
                "depth": depth,
                "child_count": len(children),
            }
            if children:
                done_c = sum(1 for c in children if c.get("status") in ("done", "skipped"))
                pending_c = _count_pending_leaves(children)
                entry["progress"] = f"{done_c}/{len(children)} direct children done, {pending_c} leaf tasks pending"
            else:
                if n.get("instruction"):
                    entry["instruction_preview"] = n["instruction"][:120] + ("…" if len(n.get("instruction", "")) > 120 else "")
                if n.get("notes"):
                    entry["notes"] = n["notes"]
            result.append(entry)
            result.extend(_flatten_for_display(children, depth + 1))
        return result

    def _all_leaves(nodes: list) -> list[dict]:
        result = []
        for n in nodes:
            children = n.get("children") or []
            if children:
                result.extend(_all_leaves(children))
            else:
                result.append(n)
        return result

    # ── Tool handlers ────────────────────────────────────────────────────────

    async def _plan_list(args: dict) -> dict:
        offset    = max(0, int(args.get("offset") or 0))
        limit     = min(50, max(1, int(args.get("limit") or 20)))
        parent_id = (args.get("parent_id") or "").strip() or None
        data      = _load()

        if parent_id:
            parent = _find_by_id(data.get("tasks", []), parent_id)
            if parent is None:
                return {"error": f"parent_id '{parent_id}' not found"}
            source = parent.get("children") or []
            context = parent.get("title", parent_id)
        else:
            source  = data.get("tasks", [])
            context = None

        flat   = _flatten_for_display(source)
        page   = flat[offset: offset + limit]
        total_leaves   = len(_all_leaves(source))
        pending_leaves = _count_pending_leaves(source)
        result = {
            "tasks": page,
            "total_items": len(flat),
            "total_leaf_tasks": total_leaves,
            "pending_leaf_tasks": pending_leaves,
            "offset": offset,
            "has_more": offset + limit < len(flat),
        }
        if context:
            result["context"] = context
        return result

    async def _plan_add(args: dict) -> dict:
        task_id     = (args.get("id") or "").strip()
        title       = (args.get("title") or "").strip()
        instruction = (args.get("instruction") or "").strip() or None
        parent_id   = (args.get("parent_id") or "").strip() or None
        notes       = (args.get("notes") or "").strip() or None
        if not task_id or not title:
            return {"error": "id and title are required"}
        data = _load()
        if _find_by_id(data.get("tasks", []), task_id):
            return {"error": f"task id '{task_id}' already exists"}
        new_task = {"id": task_id, "title": title, "status": "pending",
                    "instruction": instruction, "notes": notes, "children": []}
        # Drop None values to keep the JSON tidy
        new_task = {k: v for k, v in new_task.items() if v is not None or k in ("id", "title", "status", "children")}
        if parent_id:
            parent = _find_by_id(data.get("tasks", []), parent_id)
            if parent is None:
                return {"error": f"parent_id '{parent_id}' not found"}
            parent.setdefault("children", []).append(new_task)
        else:
            data.setdefault("tasks", []).append(new_task)
        _save(data)
        return {"added": True, "id": task_id, "pending": _count_pending_leaves(data["tasks"])}

    async def _plan_update(args: dict) -> dict:
        task_id = (args.get("id") or "").strip()
        if not task_id:
            return {"error": "id is required"}
        data = _load()
        task = _find_by_id(data.get("tasks", []), task_id)
        if task is None:
            return {"error": f"task id '{task_id}' not found"}
        _VALID_STATUSES = {"pending", "in_progress", "done", "failed", "skipped"}
        if "status" in args:
            if args["status"] not in _VALID_STATUSES:
                return {"error": f"status must be one of {sorted(_VALID_STATUSES)}"}
            task["status"] = args["status"]
        if args.get("title"):
            task["title"] = args["title"]
        if args.get("instruction"):
            task["instruction"] = args["instruction"]
        if "notes" in args:
            task["notes"] = args["notes"] or None
        # Propagate completion upward
        if args.get("status") in ("done", "skipped"):
            _auto_complete_ancestors(data.get("tasks", []), task_id)
        _save(data)
        return {"updated": True, "id": task_id,
                "pending": _count_pending_leaves(data.get("tasks", []))}

    async def _plan_remove(args: dict) -> dict:
        task_id = (args.get("id") or "").strip()
        if not task_id:
            return {"error": "id is required"}
        data = _load()
        new_tasks, removed = _remove_by_id(data.get("tasks", []), task_id)
        if not removed:
            return {"error": f"task id '{task_id}' not found"}
        data["tasks"] = new_tasks
        _save(data)
        return {"removed": True, "id": task_id,
                "pending": _count_pending_leaves(data["tasks"])}

    async def _plan_next(_args: dict) -> dict:
        data = _load()
        tasks = data.get("tasks", [])
        remaining = _count_pending_leaves(tasks)
        leaf = _next_leaf(tasks)
        if leaf is None:
            return {"task": None, "remaining": 0,
                    "message": "All tasks are done, skipped, or failed — set done=true"}
        result: dict = {"task": leaf, "remaining": max(0, remaining - 1)}
        if leaf.get("status") == "failed":
            result["retry"] = True
            result["action_required"] = "replace"
            result["warning"] = (
                f"⛔ FAILED TASK — you MUST replace it before dispatching anything else. "
                f"1) Call plan_remove(id='{leaf['id']}') to delete this task. "
                f"2) Call plan_add 2–3 smaller replacement tasks (each touching exactly 1 file) "
                f"under the same parent area — use plan_list() to find the parent_id. "
                f"The runtime will BLOCK all dispatch until this is done. "
                f"Do NOT call plan_next again until you have replaced this task."
            )
        # Surface breadcrumb so orchestrator knows which area this task belongs to
        path = _find_path_to(tasks, leaf["id"], [])
        if path and len(path) > 1:
            result["context"] = " > ".join(n.get("title", "") for n in path[:-1])
        return result

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
        "If it does not exist, create the symbol in the target file first, then import it.\n"
        "- **If a file with the same base name but different case already exists on disk** "
        "(e.g. task says `gameStateManager.js` but `GameStateManager.js` is on disk): "
        "call `grep_files` with the base name to find the exact existing path, then update THAT file. "
        "Never create a second file with different capitalization — case mismatches cause import errors.\n\n"
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
            "Start by reading the PRD for requirements context, then write all required files, run any specified commands.\n"
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
            f"INTERFACE: The exact exports, prop names, and function signatures for every module are in "
            f"`docs/INTERFACE.json` (live-generated from disk). This is already summarised in the "
            f"'## Existing module map' section of this prompt — use that first. "
            f"Do NOT search the PRD for interface details; they are not there.\n"
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


def _title_slug(title: str) -> str:
    """Convert a task title to a lowercase_snake_case slug for use as a plan node ID."""
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_") or "task"


def _planning_root_messages(prd_content: str) -> list[dict]:
    """Messages for the root planning call: PRD → top-level implementation tasks."""
    system = (
        "You are a software project planner. Read the PRD below and produce a top-level implementation plan.\n\n"
        "Use the following checklist to make sure your plan covers every applicable concern.\n"
        "You do NOT have to use these as task titles — name your tasks whatever fits the project.\n"
        "But every checked concern must be covered by at least one task in your plan.\n\n"
        "CONCERN CHECKLIST:\n"
        "  [always]  Scaffold — project files, folder structure, env config, DB schema.\n"
        "  [always]  Core Logic — domain models, state machines, business rules, algorithms. Pure, no I/O.\n"
        "  [if PRD]  Auth & Security — authentication, authorisation, session/token management, RBAC.\n"
        "  [if PRD]  Integrations — third-party APIs, OAuth providers, payment gateways, email/SMS services.\n"
        "  [if PRD]  Bespoke Frameworks — custom engine, DSL, rule interpreter, or proprietary system.\n"
        "  [if PRD]  AI/ML — model inference, embeddings, vector search, fine-tuning pipelines, prompt chains.\n"
        "  [if PRD]  Backend — server routes, API/WebSocket handlers, server-side service orchestration.\n"
        "  [if PRD]  Frontend — every UI screen, component, client-side state, routing, and rendering.\n"
        "  [if PRD]  CLI/Admin Tools — developer CLIs, management commands, admin scripts, operator tooling.\n"
        "  [if PRD]  Assets — authored content: story data, game levels, seed data, media catalogue files.\n\n"
        "RULES:\n"
        "- 5–9 tasks total. Each task covers one distinct concern from the checklist above.\n"
        "- Scaffold is always first.\n"
        "- Each concern has EXCLUSIVE OWNERSHIP of its area. A component planned in one task must NEVER\n"
        "  be re-implemented in another — later tasks reference earlier ones only as dependencies.\n"
        "- Set atomic=false for all tasks — each will be expanded into file-level sub-tasks.\n"
        "- Exception: atomic=true only if the entire task is a single file.\n\n"
        'Output JSON only: {"tasks": [{"title": "...", "atomic": false}, ...]}'
    )
    user = (
        f"## Product Requirements Document\n\n{prd_content}\n\n"
        "Produce the top-level implementation plan, ensuring every applicable concern from the checklist is covered."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# Expansion guidance injected into the expand prompt when the parent task title matches a keyword.
# These are concern-based hints, not rigid phase names — keyword matching is intentionally broad.
_PHASE_EXPAND_HINTS: list[tuple[str, str]] = [
    ("scaffold", (
        "Tasks here cover: root config files (package.json / requirements.txt / Makefile), folder structure, "
        "environment config (.env.example), DB schema/migration files, shared bootstrap utilities. "
        "Do NOT include domain logic, HTTP handlers, or UI components."
    )),
    ("core logic", (
        "Tasks here cover: domain models, state machines, rule engines, and algorithms. "
        "These must be pure and I/O-free (no HTTP, no UI, no disk writes beyond reading config). "
        "All other tasks depend on this one — define clean, well-named public interfaces."
    )),
    ("auth", (
        "Tasks here cover: user authentication flows, session/token management, password hashing, "
        "authorisation middleware, and RBAC/permission checks. "
        "CONSUME the database/storage layer from Scaffold — do NOT re-define schema here."
    )),
    ("security", (
        "Tasks here cover: user authentication flows, session/token management, password hashing, "
        "authorisation middleware, and RBAC/permission checks. "
        "CONSUME the database/storage layer from Scaffold — do NOT re-define schema here."
    )),
    ("integration", (
        "Tasks here cover: clients for external services named in the PRD (payment gateway, email provider, "
        "OAuth, third-party APIs). Each task wraps ONE external service with a clean internal interface. "
        "Do NOT re-implement business logic that belongs in Core Logic."
    )),
    ("bespoke framework", (
        "Tasks here cover the custom engine, DSL parser, or proprietary system named in the PRD. "
        "Define its public API so that Backend and Frontend tasks can consume it without knowing internals."
    )),
    ("ai", (
        "Tasks here cover: model inference clients, embedding pipelines, vector store integration, "
        "prompt chain orchestration, and any fine-tuning or evaluation tooling. "
        "Wrap each external AI service or model in a clean internal interface. "
        "CONSUME Core Logic for domain rules — do NOT re-implement business logic inside AI pipelines."
    )),
    ("ml", (
        "Tasks here cover: model inference clients, embedding pipelines, vector store integration, "
        "prompt chain orchestration, and any fine-tuning or evaluation tooling. "
        "Wrap each external AI service or model in a clean internal interface."
    )),
    ("backend", (
        "Tasks here cover: HTTP/WebSocket route handlers, middleware, and server-side service orchestration. "
        "CONSUME Core Logic components by name — do NOT re-implement their logic here. "
        "Each task should correspond to one route group, one service, or one handler module."
    )),
    ("server", (
        "Tasks here cover: HTTP/WebSocket route handlers, middleware, and server-side service orchestration. "
        "CONSUME Core Logic components by name — do NOT re-implement their logic here."
    )),
    ("api", (
        "Tasks here cover: HTTP/WebSocket route handlers, middleware, and server-side service orchestration. "
        "CONSUME Core Logic components by name — do NOT re-implement their logic here."
    )),
    ("frontend", (
        "Tasks here cover ALL user-facing screens and UI components. "
        "Plan one task per distinct screen or major UI panel (e.g. Lobby Screen, Game Map View, "
        "Inventory Panel, Chat Component, Solution Modal). "
        "Include client-side state management and routing if needed. "
        "Consume Backend APIs via network calls — do NOT re-implement server-side logic."
    )),
    ("ui", (
        "Tasks here cover ALL user-facing screens and UI components. "
        "Plan one task per distinct screen or major UI panel. "
        "Include client-side state management and routing if needed. "
        "Consume Backend APIs via network calls — do NOT re-implement server-side logic."
    )),
    ("client", (
        "Tasks here cover ALL user-facing screens and UI components. "
        "Plan one task per distinct screen or major UI panel. "
        "Consume Backend APIs via network calls — do NOT re-implement server-side logic."
    )),
    ("asset", (
        "Tasks here cover authored content files, not code. "
        "Each task produces one self-contained content unit: "
        "a story manifest + its asset folder, a level definition file, a seed data file, etc. "
        "If the PRD specifies a full example story/level, that entire story is ONE task."
    )),
    ("content", (
        "Tasks here cover authored content files, not code — story data, level definitions, "
        "seed/fixture data, media catalogue files, localisation strings. "
        "Each task produces one self-contained content unit."
    )),
    ("cli", (
        "Tasks here cover command-line interfaces, management commands, admin scripts, and operator tooling. "
        "Each task implements one CLI command group or one admin script. "
        "CONSUME Core Logic and Backend service interfaces — do NOT duplicate their logic."
    )),
    ("admin", (
        "Tasks here cover management commands, admin scripts, and operator tooling. "
        "Each task implements one CLI command group or one admin script. "
        "CONSUME Core Logic and Backend service interfaces — do NOT duplicate their logic."
    )),
]


def _phase_expand_hint(parent_title: str) -> str:
    """Return concern-specific expansion guidance based on keywords in the task title."""
    key = parent_title.lower()
    for keyword, hint in _PHASE_EXPAND_HINTS:
        if keyword in key:
            return hint
    return ""


def _planning_expand_messages(prd_content: str, parent_title: str, parent_path: str, force_leaf: bool) -> list[dict]:
    """Messages for expanding one phase into its immediate component-level tasks."""
    leaf_rule = (
        "Every task you output MUST have atomic=true and a complete 'instruction' field."
        if force_leaf else
        "Mark atomic=true for tasks that implement a single well-defined component.\n"
        "Mark atomic=false only if the task still covers multiple distinct components — it will be expanded further."
    )
    phase_hint = _phase_expand_hint(parent_title)
    phase_hint_block = f"\nPHASE GUIDANCE for '{parent_title}':\n{phase_hint}\n" if phase_hint else ""
    system = (
        f"You are a software implementation planner. Break the '{parent_title}' phase into concrete tasks.\n"
        f"{phase_hint_block}\n"
        "RULES:\n"
        "- List 3–8 tasks. Each task implements ONE component (a module, service, class, screen, or content file).\n"
        f"- {leaf_rule}\n"
        "- Do NOT specify file paths — the implementer decides file structure.\n"
        "- Each component in this phase must be UNIQUE to this phase. If a component was planned in an\n"
        "  earlier phase, reference it only as a dependency — do NOT re-plan or re-describe it here.\n"
        "- When atomic=true, 'instruction' MUST describe:\n"
        "    1. Responsibility: what this component does (1–2 sentences)\n"
        "    2. Public interface: the functions, methods, or props it exposes\n"
        "    3. Dependencies: names of components from earlier phases it consumes\n"
        "    4. Tech notes: specific library, pattern, or constraint from the PRD (if any)\n"
        "- Order tasks by dependency (foundational first).\n"
        "- Do NOT include: package installation, build steps, test suites, documentation.\n\n"
        'Output JSON only: {"tasks": [{"title": "...", "atomic": true, "instruction": "..."}, ...]}'
    )
    user = (
        f"## Phase to expand\n\nPath: {parent_path}\nPhase: {parent_title}\n\n"
        f"## Product Requirements Document\n\n{prd_content}\n\n"
        f"List the component tasks that implement the '{parent_title}' phase."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


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

    async def run_planning_stage(
        self,
        db: "AsyncSession",
        prd_content: str,
        idea: "Idea",
        branch: "SolutionBranch",
        output_dir: str,
        on_orchestrator_event: OnOrchestratorEvent,
    ) -> bool:
        """
        Recursive planning stage: PRD → root areas → expand each non-atomic node into
        sub-tasks → repeat until all nodes are atomic (one file, one concern).
        Writes .think-plan.json with the full Area > Story > Task hierarchy.
        Returns True on success; False triggers incremental fallback in the caller.
        """
        _MAX_DEPTH = 1        # root(0) → epics(1, forced atomic tasks)
        _MAX_CALLS = 40       # hard budget: prevents exponential blowup on wide trees
        _call_index = 0       # increments per model call for audit-log ordering
        _used_ids: set[str] = set()

        def _make_id(title: str) -> str:
            base = _title_slug(title)
            if base not in _used_ids:
                _used_ids.add(base)
                return base
            i = 2
            while f"{base}_{i}" in _used_ids:
                i += 1
            uid = f"{base}_{i}"
            _used_ids.add(uid)
            return uid

        plan_file = Path(output_dir) / ".think-plan.json"
        await on_orchestrator_event("orchestrator_message", {"content": "📋 Building implementation plan…"})

        async def _expand(
            parent_title: str | None,
            parent_path: list[str],
            depth: int,
        ) -> list[dict]:
            nonlocal _call_index

            if _call_index >= _MAX_CALLS:
                logger.warning("planning: call budget (%d) exhausted — skipping expansion of '%s'", _MAX_CALLS, parent_title or "root")
                return []

            path_str = " > ".join(parent_path) if parent_path else "root"
            context_label = f"'{parent_title}'" if parent_title else "root level"
            level_label = f"level {depth}" if depth > 0 else "root level"
            force_leaf = depth >= _MAX_DEPTH

            if depth == 0:
                messages = _planning_root_messages(prd_content)
            else:
                messages = _planning_expand_messages(prd_content, parent_title, path_str, force_leaf)

            logger.info("planning: %s — planning %s tasks", level_label, context_label)

            _msgs = [Message(role=m["role"], content=m["content"]) for m in messages]
            result = None
            for _attempt in range(3):
                try:
                    result = await self._client.call(
                        stage_key="phase3_planning",
                        messages=_msgs,
                        session=db,
                        idea_id=idea.id,
                        branch_id=branch.id,
                        call_type="PLANNING",
                        call_index=_call_index,
                    )
                    break
                except Exception as _retry_err:
                    if _attempt < 2:
                        logger.warning(
                            "planning: %s attempt %d/3 failed: %s — retrying",
                            context_label, _attempt + 1, _retry_err,
                        )
                        await asyncio.sleep(3)
                    else:
                        raise
            _call_index += 1

            if not isinstance(result, dict):
                raise ValueError(f"planning model returned {type(result).__name__} instead of dict")

            raw_tasks = result.get("tasks", [])
            if not raw_tasks:
                logger.warning("planning: %s — model returned no tasks", context_label)
                return []

            titles = [str(t.get("title") or t.get("id") or "?") for t in raw_tasks]
            logger.info(
                "planning: %s — planned %d task(s): %s",
                context_label, len(raw_tasks), ", ".join(titles),
            )
            await on_orchestrator_event("orchestrator_message", {
                "content": (
                    f"📋 {path_str.capitalize()}: {len(raw_tasks)} task(s) — "
                    + ", ".join(titles[:6])
                    + (" …" if len(titles) > 6 else "")
                )
            })

            nodes: list[dict] = []
            for t in raw_tasks:
                task_title = str(t.get("title") or "").strip()
                if not task_title:
                    continue

                task_id = _make_id(task_title)
                is_atomic = bool(t.get("atomic", False)) or force_leaf
                instruction = str(t.get("instruction") or "").strip() or None

                if is_atomic:
                    nodes.append({
                        "id": task_id,
                        "title": task_title,
                        "status": "pending",
                        "children": [],
                        "instruction": instruction,
                    })
                else:
                    try:
                        children = await _expand(
                            parent_title=task_title,
                            parent_path=parent_path + [task_title],
                            depth=depth + 1,
                        )
                    except Exception as _ce:
                        logger.warning(
                            "planning: failed to expand '%s' at %s: %s — keeping as leaf",
                            task_title, path_str, _ce,
                        )
                        children = []
                    nodes.append({
                        "id": task_id,
                        "title": task_title,
                        "status": "pending",
                        "children": children,
                        "instruction": instruction if not children else None,
                    })

            return nodes

        try:
            root_nodes = await _expand(parent_title=None, parent_path=[], depth=0)
        except Exception as _e:
            logger.error("planning: root expansion failed: %s", _e)
            await on_orchestrator_event("orchestrator_message", {
                "content": f"⚠ Planning failed: {_e} — orchestrator will plan incrementally."
            })
            return False

        if not root_nodes:
            logger.warning("planning: no root tasks produced — falling back to incremental")
            return False

        plan_data = {"tasks": root_nodes}
        plan_file.write_text(json.dumps(plan_data, indent=2, ensure_ascii=False), encoding="utf-8")

        def _count_leaves(nodes: list) -> int:
            return sum(
                1 if not (n.get("children") or []) else _count_leaves(n["children"])
                for n in nodes
            )

        leaf_count = _count_leaves(root_nodes)
        logger.info(
            "planning: complete — %d root area(s), %d leaf task(s) total",
            len(root_nodes), leaf_count,
        )
        await on_orchestrator_event("orchestrator_message", {
            "content": f"📋 Plan complete: {leaf_count} implementation tasks across {len(root_nodes)} areas."
        })
        return True

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
        _dispatch_errors: list[str] = []  # runtime feedback — NOT task results
        _interface_summary: str | None = None  # updated after every batch
        # Only inject follow-up context on the first round
        _initial_follow_up = follow_up_message
        _task_perm_fail_counts: dict[str, int] = {}  # per-task-id count of permanent failures
        _MAX_TASK_PERM_FAILS = 2      # block re-dispatch after this many permanent failures per task
        _MAX_ALL_FAILED_ROUNDS = 8    # abort run after this many consecutive all-failed rounds
        # Plan tasks that permanently failed and have NOT yet been replaced (removed+re-added).
        # Dispatch is blocked until each entry is gone from the plan (plan_remove was called).
        _unresolved_plan_failures: dict[str, str] = {}  # plan_task_id → title

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
            # Resume: load leaf task statuses from the nested plan tree so the orchestrator
            # knows what was already done and doesn't restart from scaffolding.
            try:
                _plan_data = json.loads(_plan_file.read_text(encoding="utf-8"))

                # Flatten ALL leaf tasks from the tree (handles both flat and nested formats)
                def _iter_leaves(nodes: list) -> "list[dict]":
                    result = []
                    for _n in nodes:
                        _ch = _n.get("children") or []
                        if _ch:
                            result.extend(_iter_leaves(_ch))
                        else:
                            result.append(_n)
                    return result

                # Build a flat map of id → node for all tasks at any depth
                def _all_nodes_map(nodes: list) -> "dict[str, dict]":
                    mp: dict = {}
                    for _n in nodes:
                        mp[_n["id"]] = _n
                        mp.update(_all_nodes_map(_n.get("children") or []))
                    return mp

                _all_leaves = _iter_leaves(_plan_data.get("tasks", []))
                _node_map = _all_nodes_map(_plan_data.get("tasks", []))
                _pending_plan_tasks: list[dict] = []

                for _pt in _all_leaves:
                    _status = _pt.get("status", "")
                    if _status == "done":
                        completed_tasks.append({
                            "id": _pt["id"],
                            "title": _pt.get("title", _pt["id"]),
                            "success": True,
                            "summary": _pt.get("notes") or "Completed in a previous run.",
                        })
                    elif _status == "failed":
                        completed_tasks.append({
                            "id": _pt["id"],
                            "title": _pt.get("title", _pt["id"]),
                            "success": False,
                            "permanently_failed": True,
                            "summary": _pt.get("notes") or "Failed in a previous run.",
                        })
                        _task_perm_fail_counts[_pt["id"]] = _MAX_TASK_PERM_FAILS
                    elif _status in ("pending", "in_progress"):
                        _pending_plan_tasks.append(_pt)

                # Auto-skip stale pending tasks based on disk state:
                # 1. Install/package tasks when node_modules already exists
                # 2. File-create tasks when the target file already exists (case-insensitive)
                import re as _re_resume
                _nm_exists = (Path(output_dir) / "node_modules").exists()
                _path_re_resume = _re_resume.compile(
                    r'[\w/.]+\.(?:js|ts|jsx|tsx|svelte|vue|py|css|html|json|yaml|toml|env)\b'
                )
                _auto_skipped_ids: set[str] = set()
                for _rpt in list(_pending_plan_tasks):
                    _instr_lc = (_rpt.get("instruction") or "").lower()
                    _is_install = any(kw in _instr_lc for kw in (
                        "npm install", "yarn install", "pnpm install",
                        "install vite", "install globally", "install -g",
                        "install dep", "install pack",
                    ))
                    if _is_install and _nm_exists:
                        _nd = _node_map.get(_rpt["id"])
                        if _nd and _nd.get("status") in ("pending", "in_progress"):
                            _nd["status"] = "skipped"
                            _auto_skipped_ids.add(_rpt["id"])
                            logger.info("orchestrator: auto-skipped install task '%s' (node_modules exists)", _rpt["id"])
                        continue
                    _instr_orig = _rpt.get("instruction") or ""
                    for _fp in _path_re_resume.findall(_instr_orig):
                        _abs_fp = Path(output_dir) / _fp
                        _file_found = _abs_fp.exists()
                        if not _file_found:
                            _par = _abs_fp.parent
                            if _par.exists():
                                try:
                                    _lc_names = {p.name.lower() for p in _par.iterdir() if p.is_file()}
                                    _file_found = _abs_fp.name.lower() in _lc_names
                                except Exception:
                                    pass
                        if _file_found:
                            _nd = _node_map.get(_rpt["id"])
                            if _nd and _nd.get("status") in ("pending", "in_progress"):
                                _nd["status"] = "skipped"
                                _auto_skipped_ids.add(_rpt["id"])
                                logger.info("orchestrator: auto-skipped task '%s' (file '%s' exists)", _rpt["id"], _fp)
                            break
                if _auto_skipped_ids:
                    _plan_file.write_text(json.dumps(_plan_data, indent=2, ensure_ascii=False), encoding="utf-8")
                    _pending_plan_tasks = [t for t in _pending_plan_tasks if t["id"] not in _auto_skipped_ids]

                _pending_titles = [_pt.get("title", _pt["id"]) for _pt in _pending_plan_tasks]
                if not completed_tasks:
                    _disk_files = sorted(
                        p.relative_to(Path(output_dir)).as_posix()
                        for p in Path(output_dir).rglob("*")
                        if p.is_file()
                        and p.name not in _INTERNAL_FILES
                        and not p.name.endswith((".log", ".pyc"))
                        and "__pycache__" not in p.parts
                    )
                    _resume_summary = (
                        "Resumed from a previous run. The project is partially implemented — do NOT re-initialize or re-scaffold. "
                        f"Files already on disk: {', '.join(_disk_files[:20])}"
                        + (f" (+{len(_disk_files) - 20} more)" if len(_disk_files) > 20 else "")
                        + ". "
                        + (f"Pending plan tasks from last run: {'; '.join(_pending_titles)}. " if _pending_titles else "")
                        + "FIRST: call plan_list() and call plan_update(id, status='skipped') for any pending task "
                        "whose file already exists on disk or whose setup is clearly already done. "
                        "Then call plan_next() and dispatch the first truly pending task."
                    )
                    completed_tasks.append({
                        "id": "_resume_context",
                        "title": "Resumed from previous run — project partially implemented",
                        "success": True,
                        "summary": _resume_summary,
                        "files_written": _disk_files[:10],
                    })
                elif _pending_plan_tasks:
                    completed_tasks.append({
                        "id": "_plan_audit_hint",
                        "title": "Resume: plan has pending tasks",
                        "success": True,
                        "summary": (
                            "Resuming with pending plan tasks: "
                            + ("; ".join(_pending_titles) if _pending_titles else "(see plan_list)")
                            + ". Before dispatching: call plan_list(), review each pending task, "
                            "call plan_update(id, status='skipped') for any already done, "
                            "then call plan_next() for the first truly pending task."
                        ),
                        "files_written": [],
                        "commands_run": [],
                    })

                logger.info(
                    "orchestrator: resume — %d completed leaf task(s), %d pending",
                    len([t for t in completed_tasks if not t["id"].startswith("_")]),
                    len(_pending_plan_tasks),
                )
            except Exception as _e:
                logger.warning("orchestrator: could not load plan for resume: %s", _e)

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

        _effective_max_rounds = _MAX_ORCHESTRATOR_ROUNDS
        for round_idx in range(_MAX_ORCHESTRATOR_ROUNDS + 50):
            # At the original ceiling, check whether the plan still has pending tasks.
            # If yes, grant 50 extension rounds and inject a continue message so the
            # orchestrator doesn't ask the user questions or signal done prematurely.
            # If no, stop — the loop ended naturally (done=true broke it earlier).
            if round_idx == _MAX_ORCHESTRATOR_ROUNDS:
                _exh_pending = False
                if output_dir:
                    _exhpf = Path(output_dir) / ".think-plan.json"
                    if _exhpf.exists():
                        try:
                            import json as _jex
                            _exhdata = _jex.loads(_exhpf.read_text(encoding="utf-8"))
                            _exh_pending = any(
                                t.get("status") in ("pending", "in_progress", "failed")
                                for t in _iter_all_plan_tasks(_exhdata.get("tasks", []))
                                if not (t.get("children") or [])
                            )
                        except Exception:
                            pass
                if not _exh_pending:
                    logger.warning(
                        "orchestrator: %d-round ceiling reached — no pending plan tasks, stopping",
                        _MAX_ORCHESTRATOR_ROUNDS,
                    )
                    break
                _effective_max_rounds = _MAX_ORCHESTRATOR_ROUNDS + 50
                logger.warning(
                    "orchestrator: %d-round ceiling reached but plan has pending tasks — "
                    "granting 50 extension rounds",
                    _MAX_ORCHESTRATOR_ROUNDS,
                )
                completed_tasks.append({
                    "id": f"_round_limit_extension_{round_idx}",
                    "title": "(round limit — auto-extend)",
                    "summary": (
                        "You exhausted the round limit but the plan still has pending tasks. "
                        "Do NOT ask the user any questions. "
                        "Call plan_list() to see which tasks remain, then dispatch them. "
                        "Continue implementing until the plan is complete, then set done=true."
                    ),
                    "success": True,
                    "files_written": [],
                    "commands_run": [],
                })
            logger.info("orchestrator: round %d/%d", round_idx + 1, _effective_max_rounds)
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
                    dispatch_errors=_dispatch_errors or None,
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
                            "REJECTED: You dispatched tasks without checking the plan first. "
                            "Call plan_list() now. If it has tasks, call plan_next() to get the first one. "
                            "If plan_list returns no tasks, THEN call plan_add() for every PRD feature "
                            "(1 file per task) before including anything in next_tasks."
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
                    # Before giving up, check whether the plan still has pending tasks.
                    # If so, inject a "continue" message and keep working rather than
                    # terminating the session mid-plan.
                    _timeout_has_pending = False
                    if output_dir:
                        _tpf = Path(output_dir) / ".think-plan.json"
                        if _tpf.exists():
                            try:
                                import json as _jt
                                _tpdata = _jt.loads(_tpf.read_text(encoding="utf-8"))
                                _timeout_has_pending = any(
                                    t.get("status") in ("pending", "in_progress", "failed")
                                    for t in _iter_all_plan_tasks(_tpdata.get("tasks", []))
                                    if not (t.get("children") or [])
                                )
                            except Exception:
                                pass
                    if _timeout_has_pending:
                        logger.info(
                            "orchestrator: pending plan tasks remain after timeout — injecting continue message"
                        )
                        completed_tasks.append({
                            "id": f"_timeout_continue_{round_idx}",
                            "title": "(user unavailable — auto-continue)",
                            "summary": (
                                "No user reply was received (unattended run). "
                                "The plan still has pending tasks. "
                                "Do NOT ask the user any questions. "
                                "Call plan_list() to see which tasks remain, then dispatch them one by one. "
                                "Continue implementing until the plan is complete, then set done=true."
                            ),
                            "success": True,
                            "files_written": [],
                            "commands_run": [],
                        })
                        continue
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

            # Load plan task map once per round for ID validation and failure tracking
            _round_plan_map: dict = {}
            _round_plan_has_pending = False
            if output_dir:
                _pf = Path(output_dir) / ".think-plan.json"
                if _pf.exists():
                    try:
                        import json as _jv
                        _pdata = _jv.loads(_pf.read_text(encoding="utf-8"))
                        _round_plan_map = {t["id"]: t for t in _iter_all_plan_tasks(_pdata.get("tasks", []))}
                        _round_plan_has_pending = any(
                            t.get("status") in ("pending", "in_progress", "failed")
                            for t in _round_plan_map.values()
                            if not (t.get("children") or [])  # only leaves
                        )
                    except Exception:
                        pass

            # Clear unresolved failures whose plan tasks have been removed or replaced.
            # plan_remove deletes the node entirely, so the id disappears from _round_plan_map.
            # plan_add under the old parent creates new children, which is also a valid resolution.
            if _unresolved_plan_failures:
                resolved_now = {
                    fid for fid in _unresolved_plan_failures
                    if fid not in _round_plan_map  # removed via plan_remove
                    or _round_plan_map[fid].get("status") != "failed"  # manually un-failed
                }
                for fid in resolved_now:
                    logger.info("orchestrator: plan failure resolved — '%s' no longer failed in plan", fid)
                    del _unresolved_plan_failures[fid]

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

                # Plan ID validation: plan_task_id must match a real plan task exactly.
                # Only enforced when the plan has been initialised with pending work.
                if _round_plan_has_pending:
                    _ptid = str(t.get("plan_task_id") or "").strip()
                    _lookup = _ptid or task_id_raw
                    if _lookup and _lookup not in _round_plan_map:
                        # The model dispatched a task with an ID that isn't in the plan.
                        # Auto-register it so the run can proceed without a tool call.
                        try:
                            import json as _jv
                            _pf2 = Path(output_dir) / ".think-plan.json"
                            _pd2 = _jv.loads(_pf2.read_text(encoding="utf-8")) if _pf2.exists() else {}

                            # Pick parent: score top-level areas by title-keyword overlap with
                            # the task title; fall back to a "Build Fixes" area (created if absent).
                            _task_words = set(str(t.get("title") or _lookup).lower().split())
                            _top_areas = [n for n in _pd2.get("tasks", []) if n.get("children") is not None]
                            _best_parent: str | None = None
                            _best_score = 0
                            for _area in _top_areas:
                                _area_words = set(str(_area.get("title") or "").lower().split())
                                _score = len(_task_words & _area_words)
                                if _score > _best_score:
                                    _best_score = _score
                                    _best_parent = _area["id"]
                            if _best_parent is None:
                                # No area matched — find or create a "Build Fixes" holding area
                                _fix_area = next(
                                    (n for n in _pd2.get("tasks", []) if n.get("id") == "_auto_build_fixes"),
                                    None,
                                )
                                if _fix_area is None:
                                    await _plan_add({"id": "_auto_build_fixes", "title": "Build Fixes"})
                                _best_parent = "_auto_build_fixes"

                            # Ensure the ID is unique — suffix with _2, _3 … if needed
                            _all_plan_ids = {nt["id"] for nt in _iter_all_plan_tasks(_pd2.get("tasks", []))}
                            _reg_id = _lookup
                            _suffix = 2
                            while _reg_id in _all_plan_ids:
                                _reg_id = f"{_lookup}_{_suffix}"
                                _suffix += 1

                            _add_result = await _plan_add({
                                "id": _reg_id,
                                "title": str(t.get("title") or _reg_id),
                                "instruction": str(t.get("instruction") or ""),
                                "parent_id": _best_parent,
                            })
                            if "error" in _add_result:
                                raise RuntimeError(_add_result["error"])

                            # Patch the task dict so dispatch uses the registered ID
                            t["plan_task_id"] = _reg_id
                            # Refresh plan map so validation below sees the new node
                            _pd2 = _jv.loads(_pf2.read_text(encoding="utf-8"))
                            _round_plan_map = {
                                nt["id"]: nt
                                for nt in _iter_all_plan_tasks(_pd2.get("tasks", []))
                            }
                            logger.info(
                                "orchestrator: auto-registered missing plan task '%s' (requested '%s') "
                                "under parent='%s' title=%r",
                                _reg_id, _lookup, _best_parent, t.get("title", ""),
                            )
                        except Exception as _ae:
                            _blocked_this_round.append(task_id_raw)
                            _dispatch_errors.append(
                                f"INVALID ID '{_lookup}': not in plan and auto-registration failed ({_ae}). "
                                f"Call plan_list() to see valid IDs, then re-dispatch."
                            )
                            logger.warning(
                                "orchestrator: blocked task '%s' — plan_task_id '%s' not in plan, auto-add failed: %s",
                                task_id_raw, _lookup, _ae,
                            )
                            continue

                # Unresolved failed plan tasks must be replaced before new work is dispatched.
                # If the orchestrator called plan_remove this round, _unresolved_plan_failures
                # will already be empty (cleared above from the reloaded plan map).
                if _unresolved_plan_failures:
                    failed_list = "; ".join(
                        f"'{fid}' (\"{title}\")"
                        for fid, title in _unresolved_plan_failures.items()
                    )
                    _blocked_this_round.append(task_id_raw)
                    _dispatch_errors.append(
                        f"DISPATCH BLOCKED — unresolved failed plan task(s): {failed_list}. "
                        f"For each: call plan_remove(id='<id>'), then plan_add 2–3 smaller replacements "
                        f"(parent_id=<area_id> from plan_list()). All dispatches are blocked until done."
                    )
                    logger.warning(
                        "orchestrator: blocked task '%s' — %d unresolved plan failure(s): %s",
                        task_id_raw, len(_unresolved_plan_failures),
                        list(_unresolved_plan_failures.keys()),
                    )
                    continue

                valid_tasks.append(t)
            # Perm-fail vetoed IDs need plan_remove; wrong-ID blocks do not
            _perm_vetoed = [
                tid for tid in _blocked_this_round
                if _task_perm_fail_counts.get(tid, 0) >= _MAX_TASK_PERM_FAILS
            ]
            if _perm_vetoed:
                _dispatch_errors.append(
                    f"PERMANENTLY VETOED (too many failures): {_perm_vetoed}. "
                    f"Call plan_remove(id) for each, then plan_add 1–2 smaller replacements "
                    f"each touching exactly ONE file. Do NOT re-dispatch these IDs."
                )
            if not valid_tasks:
                _empty_task_rounds += 1
                logger.warning(
                    "orchestrator: no valid tasks in round %d (empty #%d) — all tasks blocked or vetoed",
                    round_idx + 1, _empty_task_rounds,
                )
                _blocked_ids_str = ", ".join(repr(b) for b in _blocked_this_round[:5])
                if _empty_task_rounds <= 3 or _round_plan_has_pending:
                    completed_tasks.append({
                        "id": f"_nudge_blocked_{round_idx}",
                        "title": "(all tasks blocked — plan registration failed)",
                        "summary": (
                            f"DISPATCH ERROR (attempt {_empty_task_rounds}): All tasks you submitted were blocked. "
                            f"Blocked IDs: [{_blocked_ids_str}]. "
                            "Call plan_list() to see valid IDs. "
                            "If these are new tasks, call plan_add(id, title, parent_id) for each, "
                            "then re-dispatch using the IDs returned by plan_add()."
                        ),
                        "success": False,
                        "files_written": [],
                        "commands_run": [],
                    })
                    continue
                logger.warning(
                    "orchestrator: %d consecutive all-blocked rounds with no pending plan tasks — stopping",
                    _empty_task_rounds,
                )
                break

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
                    _plan_map = {t["id"]: t for t in _iter_all_plan_tasks(_plan_data.get("tasks", []))}
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
                            _unresolved_plan_failures[pt["id"]] = pt.get("title", pt["id"])
                            logger.debug("orchestrator: plan auto-failed '%s'", pt["id"])

                    # Propagate completion upward so parent areas auto-complete when all
                    # children are done — mirrors what plan_update does via _auto_complete_ancestors.
                    if _plan_dirty:
                        if _auto_complete_plan_parents(_plan_data.get("tasks", [])):
                            pass  # _plan_dirty already True

                    # Fallback: if a plan task was set to in_progress but the dispatched task
                    # used a completely different id (no match above), the plan task is orphaned.
                    # If this round had at least one success, mark the orphan done — the orchestrator
                    # would only mark a task in_progress immediately before dispatching it.
                    _round_had_success = any(r.get("success") for r in batch_results)
                    if _round_had_success:
                        for pt in _iter_all_plan_tasks(_plan_data.get("tasks", [])):
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
                        _plan_json_str = _json.dumps(_plan_data, indent=2, ensure_ascii=False)
                        _plan_file.write_text(_plan_json_str, encoding="utf-8")
                        session.plan_json = _plan_json_str
                        await db.commit()
                except Exception as _pe:
                    logger.warning("orchestrator: plan auto-update error: %s", _pe)

            # Every 5 rounds: full reconciliation pass — scan all completed_tasks history against
            # the plan tree to catch any updates the per-batch auto-update may have missed.
            if round_idx % 5 == 4 and output_dir and (Path(output_dir) / ".think-plan.json").exists():
                try:
                    import json as _json_rec
                    _rec_file = Path(output_dir) / ".think-plan.json"
                    _rec_data = _json_rec.loads(_rec_file.read_text(encoding="utf-8"))
                    _successful_ids: set[str] = set()
                    _failed_ids: set[str] = set()
                    for ct in completed_tasks:
                        _cid = ct.get("id", "")
                        _ptid = ct.get("plan_task_id", "")
                        if ct.get("success"):
                            _successful_ids.update(filter(None, [_cid, _ptid]))
                        elif ct.get("permanently_failed"):
                            _failed_ids.update(filter(None, [_cid, _ptid]))
                    _rec_map = {t["id"]: t for t in _iter_all_plan_tasks(_rec_data.get("tasks", []))}
                    _rec_dirty = False
                    for _tid, _pt in _rec_map.items():
                        _cur = _pt.get("status", "pending")
                        if _tid in _successful_ids and _cur not in ("done", "skipped"):
                            _pt["status"] = "done"
                            _rec_dirty = True
                            logger.info("orchestrator: plan reconcile-done '%s'", _tid)
                        elif _tid in _failed_ids and _cur not in ("done", "skipped", "failed"):
                            _pt["status"] = "failed"
                            _rec_dirty = True
                            logger.info("orchestrator: plan reconcile-failed '%s'", _tid)
                    if _rec_dirty:
                        _auto_complete_plan_parents(_rec_data.get("tasks", []))
                        _rec_json_str = _json_rec.dumps(_rec_data, indent=2, ensure_ascii=False)
                        _rec_file.write_text(_rec_json_str, encoding="utf-8")
                        session.plan_json = _rec_json_str
                        await db.commit()
                        logger.info("orchestrator: plan reconciliation complete (round %d)", round_idx + 1)
                except Exception as _re:
                    logger.warning("orchestrator: plan reconciliation error: %s", _re)

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

            # Rebuild living interface manifest after every batch
            _manifest_path = write_interface_manifest(output_dir)
            if _manifest_path:
                try:
                    _fresh_manifest = extract_interface(output_dir)
                    _interface_summary = format_manifest_summary(_fresh_manifest)
                except Exception as _exc:
                    logger.warning("orchestrator: interface summary rebuild failed: %s", _exc)

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
            "## Step 1 — read the task\n\n"
            "The user message starts with 'Task:' followed by the instruction. Read it before judging.\n\n"
            "## Step 2 — decide strictness based on task type\n\n"
            "**Scaffold / skeleton / structure / initial setup tasks** (keywords: scaffold, skeleton, "
            "structure, boilerplate, setup, init, create project, config files, directory layout, "
            "package.json, Cargo.toml, pyproject.toml, empty component, placeholder): "
            "Stubs and minimal bodies are EXPECTED. Do NOT flag `pass`, empty blocks, or "
            "`return None` in these files — the point of a scaffold task is to create the shape, "
            "not the implementation. Only flag if the file is completely empty or missing.\n\n"
            "**Implementation tasks** (anything else): apply the full checks below.\n\n"
            "## What to check for implementation tasks\n\n"
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
                        f"Task title: {task_title}\n"
                        f"Task instruction: {task_instruction}\n\n"
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
                task_category=_find_plan_category(output_dir, task_id),
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
                        max_tool_rounds=80,
                        return_json=True,
                        call_index=0,
                        on_tool_result=_wrapped_on_tool,
                        on_text_response=_on_text_response,
                        model_override=model_sm.model,
                        backend_override=model_sm.backend,
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
                                max_tool_rounds=80,
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
