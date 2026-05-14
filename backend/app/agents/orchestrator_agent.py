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
import re
import uuid
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
_BUILD_CHECK_INTERVAL = 10  # run a build check every N implementation rounds

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


async def _run_build(output_dir: str, command: str | None = None) -> dict:
    """Run the project build in output_dir and return structured results."""
    import json as _json

    if not command:
        pkg_path = Path(output_dir) / "package.json"
        pyproject_path = Path(output_dir) / "pyproject.toml"
        cargo_path = Path(output_dir) / "Cargo.toml"

        if pkg_path.exists():
            try:
                pkg = _json.loads(pkg_path.read_text(encoding="utf-8"))
                scripts = pkg.get("scripts", {})
                command = "npm run build" if "build" in scripts else "npm run build"
            except Exception:
                command = "npm run build"
        elif pyproject_path.exists():
            command = "python -m pytest --tb=short -q"
        elif cargo_path.exists():
            command = "cargo build"
        else:
            return {
                "success": None,
                "output": "No recognized build system found (package.json / pyproject.toml / Cargo.toml)",
            }

    result = await run_shell_command(command, output_dir, timeout_seconds=180)
    combined = (result.stdout + "\n" + result.stderr).strip()
    if len(combined) > 5000:
        combined = "…(truncated — showing last 5000 chars)\n" + combined[-5000:]
    return {
        "success": result.exit_code == 0,
        "exit_code": result.exit_code,
        "command": command,
        "output": combined,
        "timed_out": result.timed_out,
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


def _orchestrator_system_prompt(prd_content: str, selectable_models: list | None = None) -> str:
    prd_excerpt = prd_content[:_MAX_PRD_CHARS]
    if len(prd_content) > _MAX_PRD_CHARS:
        prd_excerpt += "\n... (PRD truncated for context)"

    if selectable_models:
        model_lines = "\n".join(
            f'- **"{m.name}"** (`{m.model}`): {m.description}'
            for m in selectable_models
        )
        model_section = (
            f"## Sub-agent model selection\n\n"
            f"Each task must include a `\"model\"` field. Pick based on task complexity:\n\n"
            f"{model_lines}\n\n"
            f"Default to **\"fast\"** when unsure — only escalate when the task genuinely requires it.\n\n"
        )
        task_schema = (
            '{"id": "snake_case_id", "title": "short title ≤ 60 chars", "model": "fast", '
            '"task_type": "implement", '
            '"instruction": "complete self-contained instructions — list all files to create, '
            'their purpose, cross-file dependencies, and commands to run"}'
        )
    else:
        model_section = ""
        task_schema = (
            '{"id": "snake_case_id", "title": "short title ≤ 60 chars", '
            '"task_type": "implement", '
            '"instruction": "complete self-contained instructions — list all files to create, '
            'their purpose, cross-file dependencies, and commands to run"}'
        )

    return (
        "You are an orchestrator agent for a software implementation pipeline.\n\n"
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
        "Do NOT call `file_edit`, `read_file`, or `run_shell` — those are reserved for sub-agents.\n\n"
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
        f"{model_section}"
        "## Output format\n\n"
        "After any tool use (or immediately), output a JSON object only — no prose:\n"
        '{"analysis": "what has been done and what remains", '
        f'"next_tasks": [{task_schema}], '
        '"done": false, "user_message": null}\n\n'
        "You may include 1–2 tasks in `next_tasks`. **Prefer 1 task per batch** — only use 2 when "
        "both tasks are truly independent and each is immediately ready to implement. "
        "Each task must be atomic: implement exactly one logical unit (one module, one component, "
        "one endpoint, one service). Aim for tasks that touch 1–3 files at most. "
        "A large feature should be split across multiple sequential batches, not crammed into one task. "
        "Tasks in the same batch MUST target completely independent files — no two tasks in a batch "
        "may write to the same file.\n\n"
        "**Task scope limit**: if a feature requires more than ~3 new functions or classes, split it "
        "into separate sequential tasks (e.g. 'Implement data models', then 'Implement business logic', "
        "then 'Implement API routes'). Smaller scope = smaller model output = complete implementations. "
        "A task instruction that names more than 3 functions is too large — split it.\n\n"
        "When all PRD sections are implemented:\n"
        '{"analysis": "all tasks complete", "next_tasks": [], "done": true, "user_message": null}\n\n'
        "When you need the user to make a decision before you can proceed:\n"
        '{"analysis": "...", "next_tasks": [], "done": false, '
        '"user_message": "the specific question or decision you need from the user"}\n\n'
        "## Implementation order — logic before UI\n\n"
        "If the PRD contains a `Logic Specification` section, you MUST implement it before any UI.\n"
        "The first task batch must produce logic-only files: pure functions or classes with no DOM, "
        "no framework imports, no rendering. These files take state as input and return new state.\n"
        "Only after the logic layer is complete may you assign tasks that touch UI, components, or rendering.\n"
        "A task that mixes logic and UI in the same file is a bug — split it.\n\n"
        "## Integration wiring — mandatory checkpoints\n\n"
        "After every 5 implementation batches, you MUST dispatch one 'Integration Wiring' task before "
        "assigning new features. This task must:\n"
        "1. Read the app entry point (App.jsx, main.py, index.js, etc.) and every file it imports directly\n"
        "2. For every `import { X } from './module'` — read that module and verify X is actually exported. "
        "If X is not exported, add the missing export or fix the import name.\n"
        "3. For React: for every `<Component prop={val} />` usage, read Component and verify it accepts "
        "`prop` with that exact name. Fix mismatches in the caller or the component.\n"
        "4. For function/method calls: verify the argument count and order match the called function's signature.\n"
        "This task WRITES real fixes — it is not exploration. Dispatch it as a real sub-agent task.\n"
        "Common wiring failures to catch: a hook file that exports plain functions instead of a React hook "
        "(missing useState/useEffect); a parent passing `config={{...}}` when a child expects `cards` and "
        "`onCardClick` as separate props; a service called with one argument when it requires two.\n\n"
        "## Build verification — use run_build\n\n"
        "You have a `run_build` tool that runs the project build command and returns compiler errors. "
        "Call it whenever you want to confirm the project compiles — especially before dispatching more "
        "feature tasks. Build error messages tell you exactly which file and line is broken and why.\n\n"
        "## task_type field\n\n"
        "Every task must include `\"task_type\": \"implement\"` (default) or `\"task_type\": \"inspect\"`.\n"
        "Use `\"inspect\"` ONLY for pure assessment tasks where the sub-agent needs to READ files and report "
        "findings — no file writes expected. Example: checking whether a module is complete, reading current "
        "state to inform planning. Use `\"implement\"` (or omit) for all tasks that create or modify files.\n"
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
        "It must record the chosen backend framework (e.g. FastAPI, Flask, Express), frontend framework "
        "(e.g. React, Svelte, Vue), database, and any other stack-level choices. "
        "Every subsequent task instruction MUST state the chosen stack explicitly — "
        "sub-agents must not make independent technology choices. "
        "Never dispatch two tasks that could independently pick conflicting frameworks "
        "(e.g. one task creates a Flask app while another creates a FastAPI app). "
        "If you detect conflicting files (two different framework entry points), dispatch a consolidation task "
        "that deletes the wrong one before any further implementation.\n\n"
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
        "## Startup scripts — mandatory before done=true\n\n"
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


def _detect_project_structure(completed_tasks: list[dict]) -> dict:
    """
    Scan files_written from completed tasks to detect established structural conventions.
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

    return {
        "source_root": "src" if src_count >= 2 else None,
        "top_dirs": sorted(top_dirs),
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

    # Accept both next_tasks (new) and next_task (legacy single-task format)
    raw_tasks = orch_result.get("next_tasks")
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


def _normalize_sub_agent_result(raw_result: object, task_title: str, task_type: str = "implement") -> dict:
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

    if "summary" not in raw_result and any(k in raw_result for k in ("message", "files", "commands")):
        message = str(raw_result.get("message") or "").strip()
        return {
            "summary": message or f"Task '{task_title}' returned a planning payload instead of an execution result.",
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

    if isinstance(success_raw, bool):
        success = success_raw
    else:
        success = False
        if not blocker:
            blocker = "Sub-agent result missing required boolean 'success' field"

    if success and blocker:
        success = False

    if success and not summary:
        summary = f"Completed task '{task_title}'."

    if success and not files_written and not commands_run and task_type != "inspect":
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
        history_block = "\n\n## Completed Tasks\n\n" + "\n".join(lines)

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
            "## Project is empty — start building immediately\n\n"
            "The output directory is empty (or contains only docs/). "
            "Do NOT call `list_files` or `inspect_files` — there is nothing to explore yet.\n\n"
            "Read the PRD in your system prompt and return a JSON object with 1–2 scaffold tasks right now. "
            "Start with the root config files (package.json / pyproject.toml / Cargo.toml, README.md, "
            ".gitignore, .env.example) and the entry point. "
            "Dispatch tasks immediately — no exploration first."
        )
    else:
        start_hint = (
            "The full project file tree is shown above — do NOT call `list_files`. "
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
        structure = _detect_project_structure(completed_tasks)
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
        f"{structure_block}"
        f"{tree_block}"
        f"{interface_block}"
        f"{services_block}"
        f"{feedback_block}\n\n"
        f"{start_hint}"
        f"{verify_block}"
    )



def _sub_agent_extra_tools() -> list:
    """Return the extra tools list for sub-agents. generate_image is included only when ComfyUI is configured."""
    from app.config import settings
    from app.inference.client import READ_PRD_TOOL, GENERATE_IMAGE_TOOL
    from app.memory import MEMORY_STORE_TOOL, MEMORY_SEARCH_TOOL, MEMORY_LIST_TOOL
    tools = [READ_PRD_TOOL, MEMORY_STORE_TOOL, MEMORY_SEARCH_TOOL, MEMORY_LIST_TOOL]
    if settings.comfyui_base_url:
        tools.append(GENERATE_IMAGE_TOOL)
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


def _sub_agent_system_prompt(task_type: str = "implement") -> str:
    if task_type == "inspect":
        mode_header = (
            "You are an INSPECTOR agent. Your only job is to READ files and return a detailed analysis.\n\n"
            "## INSPECTION MODE — read-only\n\n"
            "DO NOT write any files. DO NOT call `file_edit` or `run_shell`.\n"
            "Every response MUST contain tool calls (reads). A response with no tool calls is ALWAYS wrong.\n"
            "Returning `success: true` with empty `files_written` is CORRECT for inspection tasks.\n\n"
            "## Workflow — follow this order\n\n"
            "1. Call `memory_list()` to see what has already been analysed for this project\n"
            "2. Call `read_prd` to read the Product Requirements Document\n"
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
        "You are an EXECUTOR agent. Your only job is to call tools and write files.\n\n"
        "## You are NOT a planner\n\n"
        "Do NOT describe what you intend to do. Do NOT summarise the task. Do NOT explain your approach.\n"
        "Do NOT output the final JSON before you have written all required files.\n"
        "Every response MUST contain tool calls. A response with no tool calls is ALWAYS wrong.\n"
        "Returning `success: true` with empty `files_written` is ALWAYS wrong — "
        "unless the Nothing-to-fix rule (below) explicitly applies.\n\n"
        "## Workflow — follow this order every time\n\n"
        "1. Call `memory_list()` to see what has already been analysed for this project\n"
        "2. Call `read_prd` to read the Product Requirements Document\n"
        "3. Read context — **maximum 2 rounds**: before each `read_file`, call `memory_search` first — "
        "if a good observation exists, skip the read. Otherwise call `read_file` on the specific files "
        "named in your task, or `list_files` once if you need to confirm a path. Do NOT browse directories "
        "repeatedly. If your task names the file to write, skip `list_files` entirely and go straight to writing. "
        "`read_file` always returns `total_lines` — if the file is large, request specific ranges with "
        "`start_line`/`end_line` (e.g. `read_file(path='foo.py', start_line=200, end_line=400)`).\n"
        "   After each `read_file`, call `memory_store` with your key findings.\n"
        "4. Write all files required for your task using `file_edit` — write complete, real implementations\n"
        "5. Run any required commands (install dependencies, build, test) using `run_shell`\n"
        "6. Return a JSON summary AFTER all files are written — not before\n\n"
        "## Memory tools\n\n"
        "You have three memory tools that persist observations across all agents working on this project:\n\n"
        "- `memory_list()` — see all files already analysed. **Call this once at the start** to orient yourself.\n"
        "- `memory_search(query)` — semantic search over stored observations. "
        "Call this BEFORE reading any file — if a useful observation exists you may not need to read the file.\n"
        "- `memory_store(file_path, observation)` — store your analysis AFTER reading OR WRITING a file. "
        "Write what the file does, key exported function/class names, patterns, and anything non-obvious. "
        "Do NOT store raw file content — store your own analysis (50–200 words).\n\n"
        "**Read rule:** Before reading any file, call `memory_search` first. "
        "If a good observation already exists, skip the read.\n"
        "**Write rule:** After writing any file, call `memory_store` immediately with: the file's purpose, "
        "every exported function/class name, what it owns (e.g. 'owns all authentication logic'), "
        "and what files it imports from. This is how future agents find and reuse your work.\n"
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
) -> str:
    prior_block = ""
    if prior_failures:
        lines = []
        for f in prior_failures:
            lines.append(f"  Attempt {f['attempt']}: {f['blocker'] or f['summary']}")
        prior_block = (
            "\n\n## Previous attempts (all failed — do NOT repeat the same approach)\n\n"
            + "\n".join(lines)
            + "\n\nAnalyse what went wrong, take a different approach, and resolve the blocker."
        )
    structure_hint = ""
    if source_root and task_type != "inspect":
        structure_hint = (
            f"\n\n## Project structure constraint\n\n"
            f"This project uses `{source_root}/` as the source root. "
            f"All source files (components, utilities, modules, hooks, styles, etc.) MUST live under "
            f"`{source_root}/`. Do NOT create source files at the project root or in a different directory. "
            f"Use import paths consistent with this layout (e.g. `import Foo from './{source_root}/Foo'` "
            f"becomes `import Foo from './Foo'` when writing a file that is also inside `{source_root}/`)."
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
    if task_type == "inspect":
        closing = (
            "Read the PRD for context, then read every file listed in your task. "
            "Return a JSON summary describing the current implementation state, what is complete, "
            "and what is missing or broken. Do NOT write any files."
        )
    else:
        closing = (
            "Start by reading the PRD (including the Module Interface Contract section) and any files "
            "needed for context, then write all required files, run any specified commands. "
            "Before returning, call `read_file` on everything you wrote and fix any file that contains "
            "TODO/FIXME/placeholder text, stub implementations, or imports that don't match the contract. "
            "IMPORTANT: every function body must contain real logic — `pass`, `return None`, `return {}`, "
            "`return []`, or `...` as the ONLY body statement means the function is a stub and must be fixed. "
            "Then use `run_shell` to run the language's syntax or compile check on each file you wrote "
            "(use whatever checker the project's language and toolchain provide — the agent knows the stack "
            "from the PRD). Fix any errors before returning. "
            "After writing each file, call `memory_store` with the file's purpose and exported names. "
            "Return the JSON summary only after all files pass self-verification."
        )
    return (
        f"PROJECT: {idea_name}\n"
        f"OUTPUT DIRECTORY: {output_dir}\n"
        f"REQUIREMENTS: call `read_prd` to read the full Product Requirements Document — "
        f"do this before implementing and again after to verify compliance.\n"
        f"INTERFACE CONTRACT: The PRD contains a 'Module Interface Contract' section listing the exact "
        f"exports, prop names, and function signatures every module must implement. "
        f"Your files MUST match the contract — do not rename exports or change prop names.\n"
        f"{ownership_block}"
        f"\n\n## Your task\n\n{task_instruction}"
        f"{structure_hint}"
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
    ) -> str:
        from app import telemetry as _telemetry
        _telemetry.set_project(idea.id, idea.name)

        output_dir = session.output_dir or ""
        completed_tasks: list[dict] = []
        _interface_summary: str | None = None  # updated after every batch
        # Only inject follow-up context on the first round
        _initial_follow_up = follow_up_message
        _verification_pending = False  # True after first done=true; forces explicit PRD check rounds
        _verification_attempts = 0    # counts how many verification rounds have run
        _empty_task_rounds = 0        # consecutive rounds with done=false but no tasks produced
        _consecutive_failures = 0     # consecutive rounds that raised an exception
        _prd_sections = _extract_prd_sections(prd_content)

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
            _selectable = self._client._registry.get_stage("phase3_sub_agent").selectable_models
            orch_messages = [
                Message(role="system", content=_orchestrator_system_prompt(prd_content, _selectable)),
                Message(role="user", content=_orchestrator_user_prompt(
                    idea, branch, completed_tasks, _initial_follow_up,
                    pending_user_messages=pending_messages or None,
                    verify_prd=_verification_pending,
                    prd_sections=_prd_sections if _verification_pending else None,
                    interface_summary=_interface_summary,
                    file_tree=_file_tree,
                    services_json=_services_json,
                )),
            ]
            _initial_follow_up = None  # only include on first round

            # Periodic build check — runs every N rounds after tasks have been written
            _impl_rounds = sum(1 for t in completed_tasks if not t["id"].startswith("_"))
            if (
                not _verification_pending
                and _impl_rounds > 0
                and round_idx > 0
                and round_idx % _BUILD_CHECK_INTERVAL == 0
            ):
                logger.info("orchestrator: running periodic build check at round %d", round_idx + 1)
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

            async def _orch_tool_cb(tool: str, result: dict) -> None:
                await on_orchestrator_event("orchestrator_tool", {"tool": tool, "result": result})

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
                return await _run_build(output_dir, cmd)

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
                    max_tool_rounds=25 if _verification_pending else 20,
                    return_json=True,
                    call_index=0,
                    on_tool_result=_orch_tool_cb,
                    extra_tools=[INSPECT_FILES_TOOL, _RUN_BUILD_TOOL, MEMORY_SEARCH_TOOL, MEMORY_LIST_TOOL],
                    custom_tool_handlers={
                        "inspect_files": _handle_inspect_files,
                        "run_build": _handle_run_build,
                        **_memory_handlers(idea.id),
                    },
                )
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
                        completed_tasks.append({
                            "id": f"_nudge_{round_idx}",
                            "title": "(no tasks produced)",
                            "summary": "You returned done=false but gave no next_tasks. Call list_files/inspect_files now, then output concrete implementation tasks.",
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
            for t in next_tasks[:3]:  # cap at 3 concurrent sub-agents
                instruction = str(t.get("instruction") or "").strip()
                if not instruction:
                    logger.warning("orchestrator: task %r has empty instruction — skipping", t.get("id"))
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
            _structure = _detect_project_structure(completed_tasks)

            # Run all tasks in this batch concurrently
            batch_results = await self._run_task_batch(
                idea, branch, output_dir, valid_tasks,
                on_tool_result, on_orchestrator_event,
                source_root=_structure["source_root"],
                prd_content=prd_content,
                interface_summary=_interface_summary,
            )

            for t, sub_result in zip(valid_tasks, batch_results):
                completed_tasks.append({"id": t["_id"], "title": t["_title"], **sub_result})

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
                if result.get("success") and max_verify_cycles > 0 and task_type != "inspect":
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

        limit = self._client._registry.resources.max_parallel_sub_agents
        semaphore = asyncio.Semaphore(max(1, limit))
        logger.debug("sub_agent_batch: running %d task(s) with concurrency limit=%d", len(tasks), limit)

        async def _run_one_limited(t: dict) -> dict:
            async with semaphore:
                return await _run_one(t)

        results = await asyncio.gather(*[_run_one_limited(t) for t in tasks], return_exceptions=True)
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

            # Discard hallucinated issues: if the reported line number exceeds the
            # actual file length, the verifier made it up.
            real_issues = []
            for issue in verify["issues"]:
                _line_match = re.search(r"\bline[s]?\s+(\d+)", issue, re.IGNORECASE)
                if _line_match:
                    _claimed_line = int(_line_match.group(1))
                    # Extract the file path (first token before ':')
                    _issue_file = issue.split(":")[0].strip()
                    _abs = Path(output_dir) / _issue_file
                    if _abs.exists():
                        try:
                            _actual_lines = len(_abs.read_text(encoding="utf-8", errors="replace").splitlines())
                            if _claimed_line > _actual_lines:
                                logger.warning(
                                    "verify_fix_loop: dropping hallucinated issue (line %d > file length %d): %s",
                                    _claimed_line, _actual_lines, issue,
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
                "files_written": fix_result.get("files_written", []),
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

        # Resolve the primary model: orchestrator hint → first selectable (fast) → stage default
        selectable_map = {m.name: m.model for m in stage_cfg.selectable_models}
        if model_hint and model_hint in selectable_map:
            primary_model = selectable_map[model_hint]
        elif stage_cfg.selectable_models:
            primary_model = stage_cfg.selectable_models[0].model  # default: fast
        else:
            primary_model = stage_cfg.model

        models_to_try = [primary_model] + [m for m in stage_cfg.fallback_models if m != primary_model]
        logger.info(
            "sub_agent: starting task '%s' (%s) agent=%s model=%s (hint=%r)",
            task_title, task_id, agent_id or "?", primary_model, model_hint,
        )

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

        try:
          for attempt, model_override in enumerate(models_to_try):
            _files_edited.clear()
            _tool_counts.clear()
            # Kill any background processes left behind by the previous attempt
            # (e.g. a dev server started for build-checking that was never stopped).
            if attempt > 0:
                _bg_procs.cleanup_dir(output_dir)
            _telemetry.set_call_context(
                is_fallback=attempt > 0,
                fallback_from=models_to_try[attempt - 1] if attempt > 0 else None,
                model_type=model_hint or "fast",
                task_id=task_id,
            )
            if attempt > 0:
                logger.info("sub_agent: task '%s' — fallback attempt %d with model %s", task_title, attempt, model_override)
                await on_orchestrator_event("sub_agent_model_fallback", {
                    "task_id": task_id,
                    "model": model_override,
                    "attempt": attempt,
                })

            user_prompt = _sub_agent_user_prompt(
                task_instruction, output_dir, idea.name,
                prior_failures=prior_failures,
                source_root=source_root,
                task_type=task_type,
                interface_summary=interface_summary,
            )

            _attempt_start = _time.monotonic()
            _telemetry.suppress_next_call()  # orchestrator logs task-level outcome below
            try:
                async with AsyncSessionLocal() as sub_db:
                    last_result = await self._client.call_with_tools(
                        stage_key="phase3_sub_agent",
                        messages=[
                            Message(role="system", content=_sub_agent_system_prompt(task_type=task_type)),
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
                        model_override=model_override,
                        extra_tools=_sub_agent_extra_tools(),
                        custom_tool_handlers={
                            "read_prd": _handle_read_prd,
                            **_memory_handlers(idea.id),
                        },
                        agent_id=agent_id,
                    )
                    last_result = _normalize_sub_agent_result(last_result, task_title, task_type=task_type)

                    # Verify the model actually wrote what it claimed
                    if last_result.get("success"):
                        claimed = [f for f in (last_result.get("files_written") or []) if f]
                        if claimed and not _files_edited:
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
            _telemetry.log_call(
                stage="phase3_sub_agent",
                model=model_override,
                backend=stage_cfg.backend,
                duration_ms=int((_time.monotonic() - _attempt_start) * 1000),
                success=bool(last_result.get("success", True)) and not last_result.get("blocker"),
                error=last_result.get("blocker") or None,
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

            prior_failures.append({
                "attempt": attempt + 1,
                "blocker": last_result.get("blocker") or "",
                "summary": last_result.get("summary") or "",
            })
            logger.info("sub_agent: task '%s' attempt %d unsuccessful — blocker=%r summary=%r — %s",
                        task_title, attempt,
                        last_result.get("blocker") or "",
                        (last_result.get("summary") or "")[:120],
                        f"retrying with {models_to_try[attempt + 1]}" if attempt + 1 < len(models_to_try) else "no more fallbacks")

          return last_result
        finally:
            # Always clean up any background processes the sub-agent left running
            # (dev servers, watchers, etc.) so they don't hold ports across tasks.
            _bg_procs.cleanup_dir(output_dir)
