"""
Phase 3 Code Generator Agent — multi-pass approach.

Pass 0: JSON call → file plan ({files: [{path, description}], commands: [str]})
Pass 1-N: One call_text() per file → write immediately to disk → emit event
Final: Run each setup command → emit event
"""

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Awaitable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, Idea, Phase2Session, Phase3ActivityEvent, Phase3Session, SolutionBranch
from app.inference.base import Message
from app.inference.client import InferenceClient
from app.services.file_manager import file_manager, strip_leading_markdown_fence
from app.tools.path_utils import normalize_project_relative_path
from app.tools.shell_runner import ShellResult, run_shell_command, shell_environment_context

logger = logging.getLogger(__name__)

PLAN_STAGE_KEY    = "phase3_plan"
FILE_STAGE_KEY    = "phase3_file"
PRD_STAGE_KEY     = "phase3_prd"
PRD_PATH          = "docs/PRD.md"
MANIFEST_FILENAME = "MANIFEST.json"
MAX_COMMAND_REPAIR_ROUNDS = 2

# PRD sections generated individually to stay within output token limits.
# Third element lists which large Phase-2 docs to include — keeps each call
# focused and prevents context-window overflow on local models.
_PRD_SECTIONS: list[tuple[str, str, tuple[str, ...]]] = [
    ("Project Overview",       "What the project is, the problem it solves, and who it is for.",             ()),
    ("Requirements",           "Functional and non-functional requirements, listed clearly with bullet points.", ("resolution_summary",)),
    ("Constraints",            "Technical, resource, and business constraints.",                              ("resolution_summary",)),
    ("Solution Architecture",  "The chosen solution approach and key architectural design decisions.",         ("resolution_summary", "architecture_doc")),
    ("Components",             "Each component or service, its responsibility, and its main interfaces or APIs.", ("resolution_summary", "component_specs")),
    ("Implementation Roadmap", "Development phases, milestones, and task breakdown in order.",                ("roadmap_doc",)),
    ("Technical Decisions",    "Key decisions made during the design and Q&A phase with their rationale.",    ("resolution_summary", "architecture_doc")),
    ("Project Structure",      "The directory and file layout of the project with a brief description of each folder.", ("file_plan_summary",)),
    ("Setup & Development",    "How to install dependencies, configure the environment, run the project locally, and run tests.", ("file_plan_summary", "component_specs")),
]

# Hard cap per large doc — safety net if a single doc is enormous (~3 K tokens)
_MAX_PRD_DOC_CHARS = 12_000

# Extensions that cannot be generated as text — skip silently with a note
_BINARY_EXTENSIONS = {
    ".wasm", ".exe", ".dll", ".so", ".dylib", ".bin", ".dat",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".svg",
    ".mp3", ".mp4", ".wav", ".ogg", ".webm",
    ".zip", ".tar", ".gz", ".7z",
    ".pdf", ".ttf", ".woff", ".woff2", ".eot",
    ".pyc", ".pyo", ".class",
}

def _strip_code_fence(text: str) -> str:
    """Remove a leading markdown code fence from generated file content."""
    return strip_leading_markdown_fence(text)


def _root_script_recommendation() -> str:
    import sys as _sys
    if _sys.platform == "win32":
        return (
            "Do NOT generate a Makefile — `make` is not available on Windows. "
            "Use `pyproject.toml` (with a `[tool.scripts]` section) or a root `package.json` "
            "with `dev`, `build`, and `test` scripts instead. "
            "For multi-service projects use `docker-compose.yml`."
        )
    return (
        "One of: `Makefile`, `docker-compose.yml`, or a root `package.json` / `pyproject.toml` "
        "with `dev`, `build`, `test` scripts — whichever fits the stack"
    )


def _plan_system_prompt() -> str:
    return (
        "You are an expert software architect. Given a project specification, produce a complete, "
        "well-structured file plan for the project.\n\n"
        "## Before planning — verify package versions\n\n"
        "Your training data may be stale. Before finalising the tech stack:\n"
        "1. Use `web_search` to check the current stable version of each major library or framework "
        "you plan to use (e.g. 'React latest stable version 2025', 'FastAPI latest version', "
        "'SQLAlchemy 2.x async tutorial')\n"
        "2. Verify that the versions you choose are compatible with each other\n"
        "3. Check for any major breaking changes or deprecations since your training cutoff\n\n"
        "Only proceed to the file plan after confirming the packages and versions are current.\n\n"
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
        "## Structural anti-patterns — NEVER do these\n\n"
        "- Do NOT split the same code layer across two locations. "
        "If your project has a `server/` or `backend/` tier, ALL server-side code lives inside it. "
        "Never create both `services/` and `server/services/` — pick ONE canonical path.\n"
        "- Do NOT create two files with the same name at different directory depths "
        "(e.g., `services/gameLogic.js` AND `server/services/gameLogic.js`). "
        "One canonical path per module — duplicates always cause import confusion.\n"
        "- Every import you write in generated files must reference exactly the path shown in the file plan. "
        "Use paths relative to the file's own location.\n\n"
        "## Root-level files (always include)\n\n"
        "- `README.md` — setup, usage, and architecture overview\n"
        "- `.gitignore` — appropriate for the tech stack\n"
        "- `.env.example` — all required environment variables with placeholder values\n"
        f"- {_root_script_recommendation()}\n"
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
        f"- Command environment: {shell_environment_context()}\n"
        "- Commands: non-interactive only; install dependencies then verify the build/tests\n"
        "- Commands must be one command per string. Do not chain commands with &&, ||, or ;\n"
        "- NEVER include file or directory creation commands (mkdir, New-Item, touch, etc.) — "
        "all files are already written before commands run; creation commands will overwrite them with empty files\n"
        "- NEVER include server-start or long-running commands (npm start, npm run dev, python app.py, "
        "uvicorn, flask run, vite, etc.) — these never exit and will be misread as failures; "
        "only include commands that exit on their own (installs, builds, test runs)\n"
        "- NEVER include compiled binary outputs in the file list (.wasm, .exe, .dll, .so, .pyc, .class, "
        ".zip, .tar, etc.) — instead include the SOURCE files that compile into them (e.g. .rs, .c, .cpp "
        "for WASM/native; .java for JVM) plus a build command that produces the binary\n"
        "- NEVER include image/font/media assets (.png, .jpg, .gif, .mp3, .mp4, .ttf, .woff, etc.) — "
        "reference CDN URLs or npm packages in source code instead\n"
        "- You CAN and SHOULD write any text-based source file regardless of language: C, C++, Rust, Go, "
        "Java, Assembly, GLSL, WGSL, or any other — these are source code, not binaries\n"
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


def _prd_section_system_prompt() -> str:
    return (
        "You are a technical writer and software architect. "
        "Write a single section of a Product Requirements Document in Markdown.\n\n"
        "This document lives at `docs/PRD.md` and is the single source of truth for anyone "
        "continuing development — including other AI coding tools.\n\n"
        "Output ONLY raw Markdown for the requested section, starting with its `##` heading. "
        "Write for a developer who has never seen this project before. "
        "No preamble, no other sections, no code fences."
    )


def _prd_section_user_prompt(
    idea: Idea,
    branch: SolutionBranch,
    resolution_summary: str,
    architecture_doc: str,
    component_specs: str,
    roadmap_doc: str,
    file_plan_summary: str,
    section_name: str,
    section_scope: str,
    relevant_docs: tuple[str, ...] = (),
) -> str:
    def _trunc(text: str) -> str:
        return text if len(text) <= _MAX_PRD_DOC_CHARS else text[:_MAX_PRD_DOC_CHARS] + "\n... (truncated)"

    doc_blocks: list[str] = []
    if "resolution_summary" in relevant_docs:
        doc_blocks.append(f"TECHNICAL DECISIONS (Phase 2 Q&A):\n{_trunc(resolution_summary)}")
    if "architecture_doc" in relevant_docs:
        doc_blocks.append(f"ARCHITECTURE:\n{_trunc(architecture_doc)}")
    if "component_specs" in relevant_docs:
        doc_blocks.append(f"COMPONENT SPECIFICATIONS:\n{_trunc(component_specs)}")
    if "roadmap_doc" in relevant_docs:
        doc_blocks.append(f"IMPLEMENTATION ROADMAP:\n{_trunc(roadmap_doc)}")
    if "file_plan_summary" in relevant_docs:
        doc_blocks.append(f"PROJECT FILE STRUCTURE:\n{_trunc(file_plan_summary)}")

    docs_section = ("\n\n".join(doc_blocks) + "\n\n") if doc_blocks else ""

    return (
        f"PROJECT: {idea.name}\n"
        f"DESCRIPTION:\n{idea.description}\n\n"
        f"REQUIREMENTS:\n{idea.requirements}\n\n"
        f"CONSTRAINTS:\n{idea.constraints}\n\n"
        f"SELECTED SOLUTION (Branch {branch.branch_index}):\n"
        f"{branch.approach_summary or 'N/A'}\n\n"
        f"{docs_section}"
        f"Write ONLY the `## {section_name}` section of the PRD.\n"
        f"Scope: {section_scope}"
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


def _verify_system_prompt() -> str:
    return (
        "You are a code reviewer doing a post-generation verification pass on an auto-generated project.\n\n"
        "## Your job\n\n"
        "1. Use `list_files` and `read_file` to inspect the generated files\n"
        "2. Use `grep_files` to check cross-file consistency — e.g. that functions called in one "
        "file are actually exported from another, that import paths are correct\n"
        "3. Look ONLY for real bugs that would cause build or runtime failures:\n"
        "   - Syntax errors or malformed code\n"
        "   - Wrong, missing, or circular imports (including a file importing from itself)\n"
        "   - Functions or classes referenced but never defined, or declared twice\n"
        "   - Cross-file inconsistencies: API routes wired to the wrong handler, env vars "
        "referenced in code but missing from `.env.example`, port/host mismatches between services\n"
        "   - Obvious incomplete stubs (e.g. a function that returns `None` when it must return data)\n"
        "   - Structural issues: multiple competing package.json / build-tool trees; "
        "HTML referencing build outputs that do not match the chosen bundler "
        "(e.g. `/build/bundle.js` in a Vite app that outputs to `dist/assets/`); "
        "package.json scripts with paths that do not resolve from that file's own directory\n"
        "   - Package version incompatibilities visible in package.json "
        "(e.g. @sveltejs/vite-plugin-svelte ^1 or ^2 paired with svelte ^5; "
        "Svelte 5 component instantiation using `new App({...})` instead of `mount()`)\n"
        "4. Fix each real bug with `file_edit`. Prefer `search_replace` for targeted fixes.\n"
        "5. Do NOT rewrite entire files. Do NOT improve code style, naming, or comments. "
        "Fix only what is concretely broken.\n\n"
        "## Workflow\n\n"
        "1. List the project root to orient yourself\n"
        "2. Read package.json (or pyproject.toml / Cargo.toml) to understand the build tool and framework versions\n"
        "3. Read entry-point files (main.py, index.ts, App.tsx, server.js, src/main.js, etc.)\n"
        "4. Read the HTML entry point and verify its script/link tags match the bundler's actual output paths\n"
        "5. Follow imports to check that referenced names exist and are exported correctly\n"
        "6. Check config files for consistency with how services reference each other\n"
        "7. Fix issues as you find them, then continue reviewing\n\n"
        "## Output format\n\n"
        "Return a JSON object only — no prose, no markdown fences:\n"
        '{"message": "brief summary — what you found and fixed, or \'No issues found\'", '
        '"fixes": [{"path": "relative/path", "issue": "what was wrong", "fixed": true}]}\n\n'
        "Rules:\n"
        "- If there are no real bugs, return immediately: "
        '{"message": "No issues found.", "fixes": []}\n'
        "- Do not invent bugs. Only fix what you can concretely see is wrong.\n"
        f"- Shell environment: {shell_environment_context()}\n"
    )


def _verify_user_prompt(
    idea: Idea,
    branch: SolutionBranch,
    file_plan_summary: str,
    written_paths: list[str],
    import_issues: list[dict] | None = None,
) -> str:
    written = "\n".join(f"  {p}" for p in written_paths)
    import_block = ""
    if import_issues:
        lines = "\n".join(
            f"  - {i['file']}: imports '{i['import']}' → expected at {i['expected']} (not found on disk)"
            for i in import_issues[:20]
        )
        import_block = (
            f"\n\nSTATIC IMPORT ANALYSIS — broken relative imports detected:\n{lines}\n\n"
            "Fix these import paths as your first priority before reviewing other issues."
        )
    return (
        f"PROJECT: {idea.name}\n"
        f"DESCRIPTION: {idea.description}\n\n"
        f"SOLUTION APPROACH:\n{branch.approach_summary or 'N/A'}\n\n"
        f"PLANNED FILE STRUCTURE:\n{file_plan_summary}\n\n"
        f"FILES WRITTEN ({len(written_paths)}):\n{written}\n\n"
        f"MANIFEST: {MANIFEST_FILENAME} in the project root lists every planned file with its status.{import_block}\n\n"
        "Review the generated files for correctness. "
        "Start by listing the project root, then read the entry-point and shared-utility files. "
        "Fix any real bugs you find, then return the JSON summary."
    )


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
        "5. If the request involves adding or upgrading a library/package, use `web_search` to verify "
        "the current stable version and its API (e.g. 'axios latest version npm', "
        "'pydantic v2 migration guide') before writing code that uses it\n"
        "6. Once you have enough context, output the JSON plan\n\n"
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
        f"- Command environment: {shell_environment_context()}\n"
        "- Commands must be one command per string. Do not chain commands with &&, ||, or ;\n"
        "- NEVER include file or directory creation commands (mkdir, New-Item, touch, etc.) — "
        "all files are already written before commands run; creation commands will overwrite them with empty files\n"
        "- NEVER include server-start or long-running commands (npm start, npm run dev, python app.py, "
        "uvicorn, flask run, vite, etc.) — these never exit and will be misread as failures; "
        "only include commands that exit on their own (installs, builds, test runs)\n"
        "- NEVER include compiled binary outputs (.wasm, .exe, .dll, .so, .pyc, .zip, etc.) — "
        "include the SOURCE files instead (.rs, .c, .cpp, .java, etc.) and a build command\n"
        "- NEVER include image/font/media assets (.png, .jpg, .ttf, .woff, .mp3, etc.)\n"
        "- You CAN and SHOULD write any text-based source file regardless of language (C, Rust, Go, GLSL, etc.)\n"
        "- If no files need to change, return {\"message\":\"No files need to change.\",\"files\":[],\"commands\":[]}\n"
        "- Never answer with prose after using tools; always return the JSON object\n"
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


def _normalize_file_paths_for_output(files: list[dict], output_dir: str) -> list[dict]:
    normalized: list[dict] = []
    seen: set[str] = set()
    for file_spec in files:
        path = normalize_project_relative_path(output_dir, file_spec.get("path", ""))
        if not path or path in seen:
            continue
        seen.add(path)
        normalized.append({**file_spec, "path": path})
    return normalized


def _validate_file_plan(files: list[dict]) -> list[str]:
    """Detect structural fragmentation in the planned file list. Returns warning strings."""
    _ALLOW_MULTI = {
        "index.js", "index.ts", "index.jsx", "index.tsx", "index.py",
        "__init__.py", "README.md", "package.json", "tsconfig.json",
        ".gitignore", "Makefile", "Dockerfile", ".env.example",
    }
    warnings: list[str] = []

    # 1. Duplicate basenames at different paths
    by_name: dict[str, list[str]] = defaultdict(list)
    for f in files:
        name = Path(f.get("path", "")).name
        if name and name not in _ALLOW_MULTI:
            by_name[name].append(f["path"])
    for name, paths in by_name.items():
        if len(paths) > 1:
            warnings.append(f"Duplicate filename `{name}` planned at: {', '.join(paths)}")

    # 2. Same directory name appearing at multiple tree depths (e.g., services/ at depth 0 AND server/services/ at depth 1)
    dir_by_leaf: dict[str, set[int]] = defaultdict(set)
    for f in files:
        parts = Path(f.get("path", "")).parts[:-1]
        for depth, part in enumerate(parts):
            dir_by_leaf[part].add(depth)
    for dir_name, depths in dir_by_leaf.items():
        if len(depths) > 1:
            warnings.append(
                f"Directory `{dir_name}/` appears at tree depths {sorted(depths)} — "
                "possible duplicate structure"
            )

    return warnings


def _write_manifest(output_dir: str, files: list[dict], commands: list[str], project_name: str) -> None:
    """Write MANIFEST.json to the output root after the file plan is ready."""
    manifest = {
        "project": project_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": [
            {"path": f["path"], "description": f.get("description", ""), "status": "pending"}
            for f in files
        ],
        "commands": commands,
    }
    manifest_path = Path(output_dir) / MANIFEST_FILENAME
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _update_manifest_status(output_dir: str, file_path: str, status: str) -> None:
    """Update the status of one file entry in MANIFEST.json."""
    manifest_path = Path(output_dir) / MANIFEST_FILENAME
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in manifest.get("files", []):
            if entry.get("path") == file_path:
                entry["status"] = status
                break
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except Exception:
        pass


def _check_relative_imports(output_dir: str, written_paths: list[str]) -> list[dict]:
    """
    Parse relative JS/TS imports in written files and check if the target files exist.
    Returns a list of broken import dicts: {file, import, expected}.
    Only checks relative paths (starting with ./ or ../).
    """
    issues: list[dict] = []
    base = Path(output_dir).resolve()
    js_exts = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}
    js_files = [p for p in written_paths if Path(p).suffix.lower() in js_exts]

    import_re = re.compile(
        r"""(?:require\s*\(\s*|from\s+)['"](\.[^'"]+)['"]""",
        re.MULTILINE,
    )

    for rel_path in js_files:
        abs_path = base / rel_path
        if not abs_path.exists():
            continue
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        file_dir = abs_path.parent
        for match in import_re.finditer(content):
            import_str = match.group(1).split("?")[0].split("#")[0]
            resolved_base = (file_dir / import_str).resolve()
            found = any(
                Path(str(resolved_base) + ext).exists()
                for ext in ("", ".js", ".ts", ".jsx", ".tsx", "/index.js", "/index.ts")
            )
            if not found:
                try:
                    expected = str(resolved_base.relative_to(base)).replace("\\", "/")
                except ValueError:
                    expected = str(resolved_base)
                issues.append({"file": rel_path, "import": import_str, "expected": expected})

    return issues


def _command_failed(result: ShellResult) -> bool:
    # Timed-out commands are likely long-running servers (never exit by design) — don't treat as failures
    return result.exit_code != 0 and not result.timed_out


def _format_command_results(results: list[tuple[str, ShellResult]]) -> str:
    parts = [f"SHELL ENVIRONMENT: {shell_environment_context()}"]
    for command, result in results:
        status = "FAILED" if _command_failed(result) else "PASSED"
        parts.append(
            "\n".join([
                f"COMMAND: {command}",
                f"STATUS: {status}",
                f"EXIT_CODE: {result.exit_code}",
                f"TIMED_OUT: {result.timed_out}",
                f"STDOUT:\n{result.stdout[-6000:] or '(empty)'}",
                f"STDERR:\n{result.stderr[-6000:] or '(empty)'}",
            ])
        )
    return "\n\n---\n\n".join(parts)


def _repair_system_prompt() -> str:
    return (
        "You are an autonomous implementation repair agent. The project has already been generated "
        "or modified, and verification commands were run. Some commands failed.\n\n"
        "Use the available tools to inspect files, edit files, and rerun commands. Continue fixing "
        "until the failing commands pass or you are blocked by a missing external dependency, secret, "
        "network service, or user decision.\n\n"
        "Rules:\n"
        f"- Shell environment: {shell_environment_context()}\n"
        "- Run exactly one command per run_shell call. Do not chain commands with &&, ||, or ;\n"
        "- If a failure involves a package, import error, or version mismatch, use `web_search` to "
        "check the current package name, version, and correct import path before editing files "
        "(e.g. 'pydantic BaseSettings import v2', 'react-router-dom v6 useNavigate'). "
        "Do not guess — verify first.\n"
        "- Do not ask the user for help unless the failure cannot be fixed from the repository.\n"
        "- If a required system-level tool is missing, report that blocker instead of trying to install global packages.\n"
        "- Prefer small targeted edits based on actual command output.\n"
        "- After editing, rerun the relevant failing command with run_shell.\n"
        "- If you have edited a file and the same command fails again with the same error, STOP — "
        "report it as a blocker rather than looping. Do not edit and rerun more than twice for the same failure.\n"
        "- Return a JSON object only: "
        '{"message":"what you fixed or why you are blocked","files":[{"path":"relative/path","description":"change made"}],"commands":["commands that now pass or still fail"]}'
    )


def _repair_user_prompt(command_results: list[tuple[str, ShellResult]]) -> str:
    return (
        "The following verification/setup commands were run after Phase 3 file generation or iteration. "
        "At least one failed. Inspect the project, fix the cause, and rerun the relevant commands.\n\n"
        f"{_format_command_results(command_results)}"
    )


def _is_command_only_request(user_request: str) -> bool:
    request = user_request.lower()
    command_terms = ("run", "rerun", "re-run", "retry", "execute", "command", "test", "build", "failed")
    generation_terms = ("create", "generate", "write file", "add file", "modify", "change", "fix", "update")
    return any(term in request for term in command_terms) and not any(
        term in request for term in generation_terms
    )


def _command_request_system_prompt() -> str:
    return (
        "You are an autonomous implementation agent responding to a user request about commands, "
        "tests, builds, or running the project.\n\n"
        "CRITICAL: Always read the USER REQUEST first and treat it as the authoritative description "
        "of what is happening. If the user says something works, it works — do not contradict them "
        "based on old command output. Respond to what the user is actually asking.\n\n"
        "## Exploration workflow — ALWAYS do this first\n\n"
        "Before running any command, explore the project to understand what is actually there:\n"
        "1. Call `list_files` with path='' to see the project root layout\n"
        "2. Look for build/run entry points: `package.json`, `Makefile`, `pyproject.toml`, "
        "`requirements.txt`, `Cargo.toml`, `go.mod`, `docker-compose.yml`, etc.\n"
        "3. Call `read_file` on any entry-point file you find to learn the available scripts/targets\n"
        "4. If you encounter an unfamiliar error or a package/import problem, use `web_search` to "
        "look up the current solution before editing files "
        "(e.g. 'npm ERR peer dependency react 18', 'ModuleNotFoundError langchain 0.2'). "
        "Your training data may be stale — verify before guessing.\n"
        "5. Only after you know what files exist and what scripts they define, decide which command to run\n\n"
        "Never assume a tech stack or command based on what the project was *supposed* to generate — "
        "always verify by reading the actual files on disk.\n\n"
        "Rules:\n"
        f"- Shell environment: {shell_environment_context()}\n"
        "- Run exactly one command per run_shell call. Do not chain commands with &&, ||, or ;\n"
        "- If a command fails, read the error output carefully and fix the root cause before rerunning — "
        "do not retry the same failing command unchanged.\n"
        "- If you have edited a file and the same command fails again with the same error, STOP retrying "
        "and report it as a blocker — do not keep editing and rerunning in a loop.\n"
        "- Do not generate unrelated files.\n"
        "- If a required system-level tool is missing, report that blocker instead of trying to install global packages.\n"
        "- Do not ask the user for help unless the issue requires a secret, external service, missing system dependency, or decision.\n"
        "- IMPORTANT: if commands are still failing after your attempts, your `message` field MUST clearly "
        "describe what failed and why — do NOT return a generic success message when commands failed.\n"
        "- Return a JSON object only: "
        '{"message":"what you did and what the command result means — be explicit about failures","files":[{"path":"relative/path","description":"change made"}],"commands":["commands run"]}'
    )


def _command_request_user_prompt(
    user_request: str,
    command_results: list[tuple[str, ShellResult]],
) -> str:
    history = (
        _format_command_results(command_results)
        if command_results
        else "No previous command results are available."
    )
    return (
        f"USER REQUEST:\n{user_request}\n\n"
        "IMPORTANT: The user's message above describes the CURRENT state of the project. "
        "Trust what the user says over any previous command output — if the user says the app "
        "starts or a command works, do not treat it as failed. Address what the user is actually "
        "asking for, not what previous commands showed.\n\n"
        "PREVIOUS COMMAND HISTORY (for context only — the user's description above takes precedence):\n"
        f"{history}"
    )


class CodeGeneratorAgent:
    def __init__(self, inference_client: InferenceClient) -> None:
        self._client = inference_client

    async def _syntax_check_file(self, abs_path: str, rel_path: str, output_dir: str) -> tuple[bool, str]:
        """Run a fast syntax check on the generated file. Returns (passed, error_message)."""
        ext = Path(rel_path).suffix.lower()
        if ext in (".js", ".mjs", ".cjs"):
            r = await run_shell_command(f'node --check "{abs_path}"', working_dir=output_dir, timeout_seconds=15)
            if r.exit_code != 0 and not r.timed_out:
                return False, (r.stderr or r.stdout).strip()[:800]
        elif ext == ".py":
            r = await run_shell_command(f'python -m py_compile "{abs_path}"', working_dir=output_dir, timeout_seconds=15)
            if r.exit_code != 0 and not r.timed_out:
                return False, (r.stderr or r.stdout).strip()[:800]
        elif ext == ".json" and Path(rel_path).name not in {"package-lock.json", "yarn.lock"}:
            try:
                with open(abs_path, encoding="utf-8") as fh:
                    json.load(fh)
            except Exception as e:
                return False, str(e)[:500]
        return True, ""

    async def _generate_file_content(
        self,
        db: AsyncSession,
        idea: Idea,
        branch: SolutionBranch,
        messages: list[Message],
        call_type: str,
        call_index: int,
    ) -> str | None:
        """Generate file content with primary model + fallback chain. Returns stripped content or None."""
        fallbacks = [None] + list(self._client._registry.get_stage(FILE_STAGE_KEY).fallback_models)
        for model in fallbacks:
            try:
                raw = await self._client.call_text(
                    stage_key=FILE_STAGE_KEY,
                    messages=messages,
                    session=db,
                    idea_id=idea.id,
                    branch_id=branch.id,
                    call_type=call_type,
                    call_index=call_index,
                    model_override=model,
                )
                return _strip_code_fence(raw)
            except Exception as e:
                logger.warning("code_generator: model %s failed for index %d: %s", model or "primary", call_index, e)
        return None

    async def _run_commands(
        self,
        commands: list[str],
        output_dir: str,
        on_tool_result: Callable[[str, dict], Awaitable[None]] | None,
    ) -> list[tuple[str, ShellResult]]:
        results: list[tuple[str, ShellResult]] = []
        for command in commands:
            logger.info("code_generator: running command: %s", command)
            shell_result = await run_shell_command(command=command, working_dir=output_dir)
            results.append((command, shell_result))
            if on_tool_result:
                await on_tool_result("run_shell", {
                    "command": command,
                    "exit_code": shell_result.exit_code,
                    "stdout": shell_result.stdout,
                    "stderr": shell_result.stderr,
                    "timed_out": shell_result.timed_out,
                    "duration_ms": shell_result.duration_ms,
                })
        return results

    async def _repair_failed_commands(
        self,
        db: AsyncSession,
        idea: Idea,
        branch: SolutionBranch,
        output_dir: str,
        command_results: list[tuple[str, ShellResult]],
        on_tool_result: Callable[[str, dict], Awaitable[None]] | None,
    ) -> str:
        failed = [(command, result) for command, result in command_results if _command_failed(result)]
        if not failed:
            return ""

        repair_summaries: list[str] = []
        current_results = command_results
        for round_index in range(MAX_COMMAND_REPAIR_ROUNDS):
            failed = [(command, result) for command, result in current_results if _command_failed(result)]
            if not failed:
                break

            logger.info(
                "code_generator: repair round %d for %d failed command(s)",
                round_index + 1,
                len(failed),
            )
            repair_data = await self._client.call_with_tools(
                stage_key="phase3_explore",
                messages=[
                    Message(role="system", content=_repair_system_prompt()),
                    Message(role="user", content=_repair_user_prompt(current_results)),
                ],
                session=db,
                idea_id=idea.id,
                branch_id=branch.id,
                allowed_file_dir=output_dir,
                explore_only=False,
                max_tool_rounds=40,
                return_json=True,
                call_index=1000 + round_index,
                on_tool_result=on_tool_result,
            )

            if isinstance(repair_data, dict):
                message = str(repair_data.get("message", "")).strip()
                if message:
                    repair_summaries.append(message)

            rerun_commands = [command for command, _ in failed]
            current_results = await self._run_commands(rerun_commands, output_dir, on_tool_result)

        remaining_failures = [command for command, result in current_results if _command_failed(result)]
        if remaining_failures:
            repair_summaries.append(
                "Still failing after repair attempts: " + ", ".join(remaining_failures)
            )
        elif current_results:
            repair_summaries.append("Verification commands passed after repair.")

        return " ".join(repair_summaries)

    async def _verify_and_fix_files(
        self,
        db: AsyncSession,
        idea: Idea,
        branch: SolutionBranch,
        output_dir: str,
        file_plan_summary: str,
        written_paths: list[str],
        on_tool_result: Callable[[str, dict], Awaitable[None]] | None,
        import_issues: list[dict] | None = None,
    ) -> str:
        """Post-generation verification pass: read written files, find and fix real bugs."""
        if not written_paths:
            return ""

        if on_tool_result:
            await on_tool_result("verify_started", {"file_count": len(written_paths)})

        logger.info("code_generator: starting verification pass for %d file(s)", len(written_paths))

        result = await self._client.call_with_tools(
            stage_key="phase3_explore",
            messages=[
                Message(role="system", content=_verify_system_prompt()),
                Message(role="user", content=_verify_user_prompt(idea, branch, file_plan_summary, written_paths, import_issues)),
            ],
            session=db,
            idea_id=idea.id,
            branch_id=branch.id,
            allowed_file_dir=output_dir,
            explore_only=False,
            max_tool_rounds=50,
            return_json=True,
            call_index=2000,
            on_tool_result=on_tool_result,
        )

        if isinstance(result, dict):
            message = str(result.get("message", "")).strip()
            fixes = result.get("fixes", [])
            fix_count = len([f for f in fixes if isinstance(f, dict) and f.get("fixed")])
            if fix_count:
                logger.info("code_generator: verification fixed %d issue(s)", fix_count)
                return f"Verified {len(written_paths)} file(s); fixed {fix_count} issue(s). {message}"
            return f"Verified {len(written_paths)} file(s). {message}" if message else ""
        return ""

    async def _recent_command_results(
        self,
        db: AsyncSession,
        session: Phase3Session,
        limit: int = 6,
    ) -> list[tuple[str, ShellResult]]:
        result = await db.execute(
            select(Phase3ActivityEvent)
            .where(
                Phase3ActivityEvent.session_id == session.id,
                Phase3ActivityEvent.event_type == "command_executed",
            )
            .order_by(Phase3ActivityEvent.created_at.desc())
        )
        results: list[tuple[str, ShellResult]] = []
        for event in result.scalars():
            if len(results) >= limit:
                break
            try:
                payload = json.loads(event.payload_json)
            except json.JSONDecodeError:
                continue
            command = str(payload.get("command", "")).strip()
            if not command:
                continue
            results.append((
                command,
                ShellResult(
                    stdout=str(payload.get("stdout", "")),
                    stderr=str(payload.get("stderr", "")),
                    exit_code=int(payload.get("exit_code", -1)),
                    duration_ms=int(payload.get("duration_ms", 0)),
                    timed_out=bool(payload.get("timed_out", False)),
                ),
            ))
        return results

    async def _handle_command_request(
        self,
        db: AsyncSession,
        session: Phase3Session,
        idea: Idea,
        branch: SolutionBranch,
        user_request: str,
        output_dir: str,
        on_tool_result: Callable[[str, dict], Awaitable[None]] | None,
        chat_history: list[Message] | None = None,
    ) -> str:
        command_results = await self._recent_command_results(db, session)

        # Capture commands executed during the tool loop so we can detect failures
        loop_command_results: list[tuple[str, ShellResult]] = []

        async def _capture_and_forward(tool_name: str, payload: dict) -> None:
            if tool_name == "run_shell":
                command = str(payload.get("command", "")).strip()
                if command:
                    loop_command_results.append((
                        command,
                        ShellResult(
                            stdout=str(payload.get("stdout", "")),
                            stderr=str(payload.get("stderr", "")),
                            exit_code=int(payload.get("exit_code", -1)),
                            duration_ms=int(payload.get("duration_ms", 0)),
                            timed_out=bool(payload.get("timed_out", False)),
                        ),
                    ))
            if on_tool_result:
                await on_tool_result(tool_name, payload)

        messages = [Message(role="system", content=_command_request_system_prompt())]
        if chat_history:
            messages.extend(chat_history)
        messages.append(Message(role="user", content=_command_request_user_prompt(user_request, command_results)))

        response = await self._client.call_with_tools(
            stage_key="phase3_explore",
            messages=messages,
            session=db,
            idea_id=idea.id,
            branch_id=branch.id,
            allowed_file_dir=output_dir,
            explore_only=False,
            max_tool_rounds=50,
            return_json=True,
            call_index=2000,
            on_tool_result=_capture_and_forward,
        )

        message = ""
        if isinstance(response, dict):
            message = str(response.get("message", "")).strip()
        else:
            message = str(response).strip()

        # Run the same repair loop used by run_implementation / run_iteration
        if loop_command_results:
            repair_summary = await self._repair_failed_commands(
                db, idea, branch, output_dir, loop_command_results, on_tool_result
            )
            if repair_summary:
                message = (message + " " + repair_summary).strip() if message else repair_summary

        return message or "Command request handled."

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
        plan_data = await self._client.call_with_tools(
            stage_key=PLAN_STAGE_KEY,
            messages=plan_messages,
            session=db,
            idea_id=idea.id,
            branch_id=branch.id,
            max_tool_rounds=10,
            return_json=True,
            call_index=0,
        )

        if not isinstance(plan_data, dict):
            logger.warning("code_generator: plan stage returned non-dict: %s", type(plan_data))
            plan_data = {}
        files: list[dict] = _normalize_file_paths_for_output(
            _normalize_files(plan_data.get("files", [])),
            output_dir,
        )
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

        # Structural validation — detect duplicate trees before generation starts
        plan_warnings = _validate_file_plan(files)
        if plan_warnings:
            logger.warning("code_generator: plan has %d structural warning(s)", len(plan_warnings))
            if on_tool_result:
                await on_tool_result("plan_warnings", {"warnings": plan_warnings})

        # Persist the canonical file tree on disk so generators stay consistent
        try:
            _write_manifest(output_dir, files, commands, idea.name)
        except Exception as e:
            logger.warning("code_generator: failed to write manifest: %s", e)

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

            from pathlib import Path as _Path
            if _Path(path).suffix.lower() in _BINARY_EXTENSIONS:
                logger.info("code_generator: skipping binary file %s", path)
                if on_tool_result:
                    await on_tool_result("file_edit", {
                        "path": path, "operation": "skip_binary",
                        "success": False, "size_bytes": 0,
                        "detail": f"Skipped: binary files ({_Path(path).suffix}) cannot be generated as text",
                    })
                continue

            logger.info("code_generator: generating file %d/%d: %s", i + 1, len(files), path)
            if on_tool_result:
                await on_tool_result("pass_started", {
                    "file_path": path,
                    "file_index": i,
                    "total_files": len(files),
                })

            if path == PRD_PATH:
                try:
                    content = await self._generate_prd_chunked(
                        idea, branch, resolution_summary,
                        architecture_doc, component_specs, roadmap_doc,
                        file_plan_summary, db, call_index_base=500,
                    )
                except Exception as e:
                    logger.error("code_generator: PRD chunked generation failed: %s", e)
                    content = (
                        f"# {idea.name} — Product Requirements Document\n\n"
                        "_PRD generation failed. Re-run from the implementation panel._\n"
                    )
            else:
                file_messages = [
                    Message(role="system", content=_file_system_prompt()),
                    Message(role="user", content=_file_user_prompt(
                        idea, branch, resolution_summary, architecture_doc,
                        file_plan_summary, written_paths, path, description,
                    )),
                ]
                content = await self._generate_file_content(db, idea, branch, file_messages, "PHASE3", i + 1)
                if content is None:
                    logger.error("code_generator: all models failed for %s — skipping", path)
                    _update_manifest_status(output_dir, path, "failed")
                    continue

            abs_path = str(Path(output_dir) / path)
            result = await file_manager.write_file(abs_path, content)
            size_bytes = len(content.encode("utf-8"))

            if result.get("success"):
                files_written += 1
                written_paths.append(path)
                _update_manifest_status(output_dir, path, "written")
                logger.debug("code_generator: wrote %s (%d B)", path, size_bytes)
            else:
                _update_manifest_status(output_dir, path, "failed")
                logger.error("code_generator: failed to write %s: %s", path, result.get("error"))

            if on_tool_result:
                await on_tool_result("file_edit", {
                    "path": path,
                    "operation": "write_file",
                    "success": result.get("success", False),
                    "size_bytes": size_bytes,
                    "detail": result.get("error", ""),
                })

            # Per-file syntax check (skip PRD and binary-like files)
            if result.get("success") and path != PRD_PATH:
                syntax_ok, syntax_err = await self._syntax_check_file(abs_path, path, output_dir)
                if not syntax_ok:
                    logger.warning("code_generator: syntax error in %s — retrying once", path)
                    if on_tool_result:
                        await on_tool_result("syntax_check", {"path": path, "passed": False, "error": syntax_err, "retrying": True})
                    # Retry with error context injected
                    retry_messages = [
                        Message(role="system", content=_file_system_prompt()),
                        Message(role="user", content=(
                            _file_user_prompt(
                                idea, branch, resolution_summary, architecture_doc,
                                file_plan_summary, written_paths, path, description,
                            )
                            + f"\n\nPREVIOUS ATTEMPT HAD A SYNTAX ERROR — fix it:\n{syntax_err}"
                        )),
                    ]
                    retry_content = await self._generate_file_content(db, idea, branch, retry_messages, "PHASE3", i + 1)
                    if retry_content:
                        retry_result = await file_manager.write_file(abs_path, retry_content)
                        if retry_result.get("success"):
                            syntax_ok2, syntax_err2 = await self._syntax_check_file(abs_path, path, output_dir)
                            if on_tool_result:
                                await on_tool_result("syntax_check", {"path": path, "passed": syntax_ok2, "error": syntax_err2, "retrying": False})
                            if syntax_ok2:
                                logger.info("code_generator: syntax retry succeeded for %s", path)
                        else:
                            if on_tool_result:
                                await on_tool_result("syntax_check", {"path": path, "passed": False, "error": "retry write failed", "retrying": False})
                    else:
                        if on_tool_result:
                            await on_tool_result("syntax_check", {"path": path, "passed": False, "error": "retry generation failed", "retrying": False})

        # ── Import analysis: feed broken relative imports to the verifier ─────
        import_issues: list[dict] = []
        try:
            import_issues = _check_relative_imports(output_dir, written_paths)
            if import_issues:
                logger.info("code_generator: %d broken relative import(s) detected pre-verification", len(import_issues))
        except Exception as e:
            logger.warning("code_generator: import analysis failed: %s", e)

        # ── Verification: read written files and fix any real bugs ───────────
        verify_summary = await self._verify_and_fix_files(
            db, idea, branch, output_dir,
            file_plan_summary, written_paths, on_tool_result,
            import_issues=import_issues or None,
        )

        # ── Final: run setup commands ─────────────────────────────────────────
        command_results = await self._run_commands(commands, output_dir, on_tool_result)
        repair_summary = await self._repair_failed_commands(
            db, idea, branch, output_dir, command_results, on_tool_result
        )

        file_list = ", ".join(written_paths[:5])
        if len(written_paths) > 5:
            file_list += f", … ({len(written_paths) - 5} more)"
        verify_part = f" {verify_summary}" if verify_summary else ""
        cmd_summary = f" Ran {len(commands)} setup command(s)." if commands else ""
        if repair_summary:
            cmd_summary += f" {repair_summary}"
        return (
            f"Wrote {files_written}/{len(files)} file(s): {file_list}.{verify_part}{cmd_summary} "
            f"Project is at: {output_dir}"
        )

    async def _generate_prd_chunked(
        self,
        idea: Idea,
        branch: SolutionBranch,
        resolution_summary: str,
        architecture_doc: str,
        component_specs: str,
        roadmap_doc: str,
        file_plan_summary: str,
        db: AsyncSession,
        call_index_base: int = 500,
    ) -> str:
        """Generate the PRD section-by-section to stay within output token limits."""
        sections: list[str] = []
        for i, (section_name, section_scope, relevant_docs) in enumerate(_PRD_SECTIONS):
            try:
                text = await self._client.call_text(
                    stage_key=PRD_STAGE_KEY,
                    messages=[
                        Message(role="system", content=_prd_section_system_prompt()),
                        Message(role="user", content=_prd_section_user_prompt(
                            idea, branch, resolution_summary,
                            architecture_doc, component_specs, roadmap_doc,
                            file_plan_summary, section_name, section_scope,
                            relevant_docs,
                        )),
                    ],
                    session=db,
                    idea_id=idea.id,
                    branch_id=branch.id,
                    call_type="PHASE3",
                    call_index=call_index_base + i,
                )
                sections.append(_strip_code_fence(text))
                logger.debug("PRD section '%s' generated (%d chars)", section_name, len(text))
            except Exception as e:
                logger.warning("PRD section '%s' failed: %s — using placeholder", section_name, e)
                sections.append(f"## {section_name}\n\n_Section could not be generated._\n")

        header = f"# {idea.name} — Product Requirements Document\n\n"
        return header + "\n\n".join(sections)

    async def generate_prd(
        self,
        db: AsyncSession,
        session: Phase3Session,
        idea: Idea,
        branch: SolutionBranch,
        on_tool_result: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> bool:
        """Regenerate docs/PRD.md from Phase 2 docs. Returns True on success."""
        resolution_summary = await self._load_resolution_summary(db, idea)
        architecture_doc = await self._load_doc(db, branch, "ARCHITECTURE_OVERVIEW")
        component_specs = await self._load_doc(db, branch, "COMPONENT_SPECS")
        roadmap_doc = await self._load_doc(db, branch, "IMPLEMENTATION_ROADMAP")
        output_dir = session.output_dir or ""

        # Build file plan summary from what's already on disk (best-effort)
        file_plan_summary = ""
        try:
            base = Path(output_dir)
            if base.is_dir():
                paths = [
                    str(p.relative_to(base)).replace("\\", "/")
                    for p in sorted(base.rglob("*"))
                    if p.is_file()
                ]
                file_plan_summary = "\n".join(f"  {p}" for p in paths)
        except Exception:
            pass

        content = await self._generate_prd_chunked(
            idea, branch, resolution_summary, architecture_doc,
            component_specs, roadmap_doc, file_plan_summary, db,
        )

        abs_path = str(Path(output_dir) / PRD_PATH)
        result = await file_manager.write_file(abs_path, content)
        size_bytes = len(content.encode("utf-8"))

        if on_tool_result:
            await on_tool_result("file_edit", {
                "path": PRD_PATH,
                "operation": "write_file",
                "success": result.get("success", False),
                "size_bytes": size_bytes,
                "detail": result.get("error", ""),
            })

        if result.get("success"):
            logger.info("generate_prd: wrote %s (%d B)", PRD_PATH, size_bytes)
            return True
        else:
            logger.error("generate_prd: failed to write PRD: %s", result.get("error"))
            return False

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
        chat_history: list[Message] | None = None,
    ) -> str:
        """
        Iteration pass: given a user change request, produce only the affected files.
        Returns a plain-text summary of what changed.
        """
        output_dir = session.output_dir or ""
        previous_summary = session.summary or "No previous summary."

        if _is_command_only_request(user_request):
            return await self._handle_command_request(
                db, session, idea, branch, user_request, output_dir, on_tool_result,
                chat_history=chat_history,
            )

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
            max_tool_rounds=None,
            return_json=True,
        )

        if not isinstance(plan_data, dict):
            logger.warning("iteration: explore stage returned non-dict: %s", type(plan_data))
            plan_data = {}
        files: list[dict] = _normalize_file_paths_for_output(
            _normalize_files(plan_data.get("files", [])),
            output_dir,
        )
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

            if Path(path).suffix.lower() in _BINARY_EXTENSIONS:
                logger.info("code_generator: skipping binary file %s", path)
                if on_tool_result:
                    await on_tool_result("file_edit", {
                        "path": path, "operation": "skip_binary",
                        "success": False, "size_bytes": 0,
                        "detail": f"Skipped: binary files ({Path(path).suffix}) cannot be generated as text",
                    })
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
            iter_user_prompt = (
                f"PROJECT: {idea.name}\n\n"
                f"USER REQUEST: {user_request}\n\n"
                f"{existing_block}"
                f"CHANGE REQUIRED: {description}\n\n"
                f"Write the complete updated content of `{path}` now."
            )
            file_messages = [
                Message(role="system", content=_file_system_prompt()),
                Message(role="user", content=iter_user_prompt),
            ]
            content = await self._generate_file_content(db, idea, branch, file_messages, "PHASE3_ITER", i + 1)
            if content is None:
                logger.error("iteration: all models failed for %s — skipping", path)
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

            # Per-file syntax check in iterations too
            if result.get("success"):
                syntax_ok, syntax_err = await self._syntax_check_file(abs_path, path, output_dir)
                if not syntax_ok:
                    logger.warning("iteration: syntax error in %s — retrying once", path)
                    if on_tool_result:
                        await on_tool_result("syntax_check", {"path": path, "passed": False, "error": syntax_err, "retrying": True})
                    retry_messages = [
                        Message(role="system", content=_file_system_prompt()),
                        Message(role="user", content=iter_user_prompt + f"\n\nPREVIOUS ATTEMPT HAD A SYNTAX ERROR — fix it:\n{syntax_err}"),
                    ]
                    retry_content = await self._generate_file_content(db, idea, branch, retry_messages, "PHASE3_ITER", i + 1)
                    if retry_content:
                        retry_result = await file_manager.write_file(abs_path, retry_content)
                        if retry_result.get("success"):
                            syntax_ok2, syntax_err2 = await self._syntax_check_file(abs_path, path, output_dir)
                            if on_tool_result:
                                await on_tool_result("syntax_check", {"path": path, "passed": syntax_ok2, "error": syntax_err2, "retrying": False})
                    else:
                        if on_tool_result:
                            await on_tool_result("syntax_check", {"path": path, "passed": False, "error": "retry generation failed", "retrying": False})

        command_results = await self._run_commands(commands, output_dir, on_tool_result)
        repair_summary = await self._repair_failed_commands(
            db, idea, branch, output_dir, command_results, on_tool_result
        )

        changed = ", ".join(written_paths[:5])
        if len(written_paths) > 5:
            changed += f", … ({len(written_paths) - 5} more)"
        summary = f"Updated {files_written} file(s): {changed}." if files_written else "No files were written."
        if repair_summary:
            summary += f" {repair_summary}"
        return summary

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
