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
from app.inference.client import InferenceClient, INSPECT_FILES_TOOL, READ_PRD_TOOL
from app.tools.shell_runner import shell_environment_context

logger = logging.getLogger(__name__)

_MAX_ORCHESTRATOR_ROUNDS = 100  # safety ceiling — real stops are done=true or consecutive empty rounds
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
        "- `inspect_files`: read up to 10 files at once and return their content (truncated) plus "
        "stub-detection markers. Use this to inspect multiple files in ONE round — far more efficient "
        "than individual file reads. Pass up to 10 file paths at a time.\n"
        "- `grep_files`: search across files for a pattern\n"
        "Do NOT call `file_edit`, `read_file`, or `run_shell` — those are reserved for sub-agents.\n\n"
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
        "## Implementation order — logic before UI\n\n"
        "If the PRD contains a `Logic Specification` section, you MUST implement it before any UI.\n"
        "The first task batch must produce logic-only files: pure functions or classes with no DOM, "
        "no framework imports, no rendering. These files take state as input and return new state.\n"
        "Only after the logic layer is complete may you assign tasks that touch UI, components, or rendering.\n"
        "A task that mixes logic and UI in the same file is a bug — split it.\n\n"
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
        "## Critical rule — what sub-agent tasks ARE and ARE NOT\n\n"
        "Sub-agent tasks exist for ONE purpose only: **writing files and running commands**.\n"
        "They cannot help you explore the project — exploration is YOUR job via your own tools.\n\n"
        "FORBIDDEN task titles/purposes (these will be rejected automatically):\n"
        "- 'List files', 'List all files', 'Explore project', 'Read existing files', 'Inspect structure'\n"
        "- Any task whose sole purpose is to read, list, grep, or inspect — no file writing, no commands\n\n"
        "If you need to see the project state, call `list_files` or `inspect_files` RIGHT NOW in this response "
        "before deciding tasks. Do NOT create a sub-agent to do it for you.\n\n"
        "## Rules\n\n"
        "- Delegate 1–3 cohesive, independent tasks per response\n"
        "- Before choosing tasks, call `list_files` to see what is on disk, then `inspect_files` on relevant files\n"
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


def _orchestrator_user_prompt(
    idea: Idea,
    branch: SolutionBranch,
    completed_tasks: list[dict],
    follow_up_message: str | None = None,
    pending_user_messages: list[str] | None = None,
    verify_prd: bool = False,
    prd_sections: list[str] | None = None,
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
            "Call `list_files` to check what is on disk, then `inspect_files` on any files you suspect "
            "are incomplete, then decide the next task.\n\n"
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
        feedback_block = f"\n\n## User feedback (received while last batch was running)\n\n{msgs}\n\nIncorporate this feedback into your next task decisions."

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
            "6. Spawn a build verification task: run the project build command "
            "(e.g. `npm run build`, `pip install -e .`, `cargo build`) and confirm it exits 0.\n"
            "   If it fails, add repair tasks and set `done=false`.\n"
            "7. For every section classified PARTIAL or MISSING — add tasks to complete it "
            "and set `done=false`. Only sections with quoted evidence count as IMPLEMENTED.\n"
            "8. Only set `done=true` when ALL sections are IMPLEMENTED with evidence and the build succeeds."
        )

    return (
        f"PROJECT: {idea.name}\n"
        f"DESCRIPTION: {idea.description}\n\n"
        f"SELECTED APPROACH: {branch.approach_summary or 'N/A'}\n"
        f"{history_block}"
        f"{structure_block}"
        f"{feedback_block}\n\n"
        f"{start_hint}"
        f"{verify_block}"
    )



def _sub_agent_system_prompt() -> str:
    return (
        "You are a sub-agent implementing a specific task in a software project.\n\n"
        "## Workflow — follow this order every time\n\n"
        "1. Call `read_prd` to read the Product Requirements Document\n"
        "2. Read any existing files you need for context (`list_files`, `read_file`, `grep_files`)\n"
        "3. Write all files required for your task using `file_edit`\n"
        "4. Run any required commands (install dependencies, build, test) using `run_shell`\n"
        "5. **Self-verify every file you wrote — MANDATORY, do not skip:**\n"
        "   a. Call `read_file` on each file you wrote and scan the content for: TODO, FIXME, "
        "placeholder comments, `raise NotImplementedError`, ellipsis (`...`), or any stub markers.\n"
        "   b. If any are found — rewrite that file now with the complete real implementation.\n"
        "   c. Call `read_prd` and confirm every PRD requirement in your task scope is satisfied.\n"
        "   d. Only return `success: true` after every file is verified stub-free.\n"
        "6. Return a JSON summary when done\n\n"
        "## File writing rules\n\n"
        "- Write complete file content — never truncate, use ellipsis, or leave TODO/FIXME/placeholder text\n"
        "- Returning `success: true` with any stub, TODO, FIXME, `raise NotImplementedError`, "
        "placeholder comment, or truncated section is a hard failure — the orchestrator will reject it\n"
        "- **Prefer small, focused files over large monolithic ones.** If a file would exceed ~150 lines, "
        "split it into logical modules (e.g. separate utils, hooks, components, constants). "
        "Smaller files are easier to write completely in one pass and less likely to be truncated.\n"
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
        "- Never run servers or long-running processes (`npm run dev`, `npm start`, `python -m uvicorn`, etc.)\n"
        "- **Never run `npm install`, `yarn install`, `pnpm install`, `pip install`, or any package manager install "
        "command unless your task instruction EXPLICITLY tells you to.** When multiple sub-agents run in parallel, "
        "concurrent installs collide and corrupt the node_modules / virtualenv. The orchestrator assigns install "
        "responsibility to exactly one task — if yours does not mention it, skip it.\n"
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


def _sub_agent_user_prompt(
    task_instruction: str, output_dir: str, idea_name: str,
    prior_failures: list[dict] | None = None,
    source_root: str | None = None,
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
    if source_root:
        structure_hint = (
            f"\n\n## Project structure constraint\n\n"
            f"This project uses `{source_root}/` as the source root. "
            f"All source files (components, utilities, modules, hooks, styles, etc.) MUST live under "
            f"`{source_root}/`. Do NOT create source files at the project root or in a different directory. "
            f"Use import paths consistent with this layout (e.g. `import Foo from './{source_root}/Foo'` "
            f"becomes `import Foo from './Foo'` when writing a file that is also inside `{source_root}/`)."
        )
    return (
        f"PROJECT: {idea_name}\n"
        f"OUTPUT DIRECTORY: {output_dir}\n"
        f"REQUIREMENTS: call `read_prd` to read the full Product Requirements Document — "
        f"do this before implementing and again after to verify compliance.\n"
        f"\n\n## Your task\n\n{task_instruction}"
        f"{structure_hint}"
        f"{prior_block}\n\n"
        "Start by reading any files needed for context, then write all required files, "
        "run any specified commands. Before returning, call `read_file` on everything you wrote "
        "and fix any file that contains TODO/FIXME/placeholder text or stub implementations. "
        "Return the JSON summary only after all files pass self-verification."
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
            orch_messages = [
                Message(role="system", content=_orchestrator_system_prompt(prd_content)),
                Message(role="user", content=_orchestrator_user_prompt(
                    idea, branch, completed_tasks, _initial_follow_up,
                    pending_user_messages=pending_messages or None,
                    verify_prd=_verification_pending,
                    prd_sections=_prd_sections if _verification_pending else None,
                )),
            ]
            _initial_follow_up = None  # only include on first round

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

            try:
                orch_result = await self._client.call_with_tools(
                    stage_key="phase3_verification" if _verification_pending else "phase3_orchestrator",
                    messages=orch_messages,
                    session=db,
                    idea_id=idea.id,
                    branch_id=branch.id,
                    allowed_file_dir=output_dir,
                    explore_only=True,
                    # Orchestrator: 12 rounds — list_files (1) + inspect_files batches (up to
                    # 5) + dispatch (1), with a few spare. Forces task commitment.
                    # Verification: 15 rounds — needs to inspect all files then classify.
                    max_tool_rounds=15 if _verification_pending else 12,
                    return_json=True,
                    call_index=round_idx * 100,
                    on_tool_result=_orch_tool_cb,
                    extra_tools=[INSPECT_FILES_TOOL],
                    custom_tool_handlers={"inspect_files": _handle_inspect_files},
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
                title_lower = str(t.get("title") or "").lower().strip()
                instruction_lower = instruction.lower()
                is_explore_only = (
                    any(title_lower.startswith(p) for p in _EXPLORE_PREFIXES)
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

            # Emit started events for all tasks upfront
            for t in valid_tasks:
                task_id = str(t.get("id") or f"task_{round_idx}")
                task_title = str(t.get("title") or "Task")[:80]
                t["_id"] = task_id
                t["_title"] = task_title
                await on_orchestrator_event("sub_agent_started", {"task_id": task_id, "title": task_title})

            # Detect established source layout so sub-agents stay consistent
            _structure = _detect_project_structure(completed_tasks)

            # Run all tasks in this batch concurrently
            batch_results = await self._run_task_batch(
                idea, branch, output_dir, valid_tasks,
                on_tool_result, on_orchestrator_event,
                source_root=_structure["source_root"],
                prd_content=prd_content,
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
        source_root: str | None = None,
        prd_content: str | None = None,
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
                    source_root=source_root,
                    prd_content=prd_content,
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
                has_stubs = any(m.lower() in text.lower() for m in _STUB_MARKERS)

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

        async def _handle_read_prd(_args: dict) -> dict:
            return {"prd": prd_content or "", "length": len(prd_content or "")}

        stage_cfg = self._client._registry.get_stage("phase3_sub_agent")
        models_to_try: list[str | None] = [None] + list(stage_cfg.fallback_models)
        last_result: dict = {}
        prior_failures: list[dict] = []

        for attempt, model_override in enumerate(models_to_try):
            if model_override:
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
            )

            try:
                async with AsyncSessionLocal() as sub_db:
                    last_result = await self._client.call_with_tools(
                        stage_key="phase3_sub_agent",
                        messages=[
                            Message(role="system", content=_sub_agent_system_prompt()),
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
                        model_override=model_override,
                        extra_tools=[READ_PRD_TOOL],
                        custom_tool_handlers={"read_prd": _handle_read_prd},
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

            prior_failures.append({
                "attempt": attempt + 1,
                "blocker": last_result.get("blocker") or "",
                "summary": last_result.get("summary") or "",
            })
            logger.info("sub_agent: task '%s' attempt %d unsuccessful (success=false), %s",
                        task_title, attempt,
                        f"retrying with {models_to_try[attempt + 1]}" if attempt + 1 < len(models_to_try) else "no more fallbacks")

        return last_result
