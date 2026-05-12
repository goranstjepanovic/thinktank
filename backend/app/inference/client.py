import asyncio
import json
import logging
import pathlib as _pathlib
import re
import uuid
from datetime import datetime, timezone

from app.tools.path_utils import normalize_project_relative_path

logger = logging.getLogger(__name__)

try:
    from app.tools.shell_runner import shell_environment_context
except Exception:
    def shell_environment_context() -> str:
        return "OS and CLI context unavailable. Prefer one command per run_shell call."


def _strip_markdown_json(text: str) -> str:
    """
    Strip <think> blocks, chat tokens, and markdown code fences from a model
    response so the content can be parsed as JSON.
    """
    stripped = text.strip()
    # Strip <think>...</think> blocks produced by reasoning models (e.g. qwen3.5)
    stripped = re.sub(r"<think>.*?</think>", "", stripped, flags=re.DOTALL).strip()
    # Strip Qwen/Yi chat special tokens that sometimes leak into content
    stripped = re.sub(r"<\|im_start\|>.*?(\{)", r"\1", stripped, count=1, flags=re.DOTALL).strip()
    stripped = re.sub(r"<\|im_end\|>", "", stripped).strip()
    # If the response already looks like JSON, do not scan for code fences.
    if stripped.startswith("{") or stripped.startswith("["):
        return stripped
    # Match ```json or ``` at the start, capture everything until closing ```
    m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", stripped, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Sometimes the model emits prose before the fence — grab the first fence block
    m = re.search(r"(?:^|\n)```(?:json)?\s*\n?(.*?)\n?```", stripped, re.DOTALL)
    if m:
        return m.group(1).strip()
    return stripped

from sqlalchemy.ext.asyncio import AsyncSession

from app.inference.base import (
    InferenceBackend,
    InferenceBackendError,
    InferenceRequest,
    InferenceResponse,
    Message,
    ToolCall,
    ToolDefinition,
)
from app.inference.model_registry import ModelRegistry

# ---------------------------------------------------------------------------
# Tool definition exposed to all pipeline stages that opt-in
# ---------------------------------------------------------------------------

RUN_PYTHON_TOOL = ToolDefinition(
    name="run_python",
    description=(
        "Execute a Python script and return stdout, stderr, and exit code. "
        "Use this for calculations, numerical estimates, data parsing, "
        "complexity analysis, or any task that benefits from exact computation "
        "rather than inference. The script runs in a sandboxed subprocess with "
        "no network access. Available stdlib: math, json, csv, statistics, "
        "itertools, collections, functools, re, datetime, decimal, fractions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "Python source code to execute. Print results to stdout.",
            }
        },
        "required": ["script"],
    },
)

WEB_SEARCH_TOOL = ToolDefinition(
    name="web_search",
    description=(
        "Search the web for current, factual information. "
        "Use this to verify that libraries, frameworks, APIs, or hardware components exist and are actively maintained; "
        "to check current best practices, version compatibility, or benchmarks; "
        "to find recent security advisories or deprecation notices; "
        "or to gather any technical facts needed for rigorous analysis. "
        "Prefer specific, technical queries over broad ones. "
        "Examples: 'whisper.cpp Raspberry Pi 5 performance benchmarks', "
        "'FastAPI async SQLAlchemy 2.0 compatibility', 'React Server Components production support 2024'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query. Be specific and technical.",
            }
        },
        "required": ["query"],
    },
)


SHELL_TOOL = ToolDefinition(
    name="run_shell",
    description=(
        "Execute a shell command in the project directory. "
        "Use this to install dependencies (npm install, pip install, etc.), "
        "run build tools (tsc, dotnet build, cargo build), run tests (pytest, npm test), "
        "initialise project scaffolding (npx create-react-app, etc.), or verify the project works. "
        "The command runs in the project output directory as the working directory. "
        "Output is returned so you can check for errors and fix them. "
        f"Runtime context: {shell_environment_context()} "
        "Run exactly one command per tool call; do not chain commands with &&, ||, or ;. "
        "Avoid commands that require interactive input."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute (e.g. 'npm install', 'pip install -r requirements.txt', 'pytest').",
            },
        },
        "required": ["command"],
    },
)

LIST_FILES_TOOL = ToolDefinition(
    name="list_files",
    description=(
        "List the files and directories inside a project directory. "
        "Use this to understand the project layout before reading specific files. "
        "Pass an empty path or '.' to list the project root."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path within the project to list (e.g. 'src', 'backend/app'). Use '' or '.' for root.",
            },
        },
        "required": [],
    },
)

READ_FILE_TOOL = ToolDefinition(
    name="read_file",
    description=(
        "Read a file's contents, optionally within a line range. "
        "Without start_line/end_line: returns the first 200 lines plus total_lines and size_bytes — "
        "check total_lines to know if you need more reads. "
        "With start_line and end_line: returns exactly those lines (0-indexed, end_line exclusive). "
        "Use this to read large files in chunks, e.g. read_file(path, 200, 400) for lines 200–399."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (e.g. 'backend/app/main.py').",
            },
            "start_line": {
                "type": "integer",
                "description": "First line to read, 0-indexed. Omit to start from line 0.",
            },
            "end_line": {
                "type": "integer",
                "description": "Exclusive end line (e.g. 400 returns lines 0–399). Omit to read one 200-line chunk from start_line.",
            },
        },
        "required": ["path"],
    },
)

GREP_FILES_TOOL = ToolDefinition(
    name="grep_files",
    description=(
        "Search for a text pattern across all project files. "
        "Returns matching lines with file paths and line numbers. "
        "Use this to locate functions, imports, variable names, or any specific string across the codebase."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "The text or substring to search for (case-sensitive).",
            },
            "path": {
                "type": "string",
                "description": "Relative directory to search within. Use '' or '.' to search the whole project.",
            },
            "file_glob": {
                "type": "string",
                "description": "Optional filename glob to restrict search (e.g. '*.py', '*.ts'). Leave empty to search all text files.",
            },
        },
        "required": ["pattern"],
    },
)

FILE_EDIT_TOOL = ToolDefinition(
    name="file_edit",
    description=(
        "Write or modify a file in the project output directory. "
        "All paths are relative to the project root and are restricted to the project directory. "
        "Operations: "
        "'write_file' — write complete content (creates or overwrites the whole file); "
        "'search_replace' — find exact text and replace it (first occurrence unless replace_all=true). "
        "Prefer search_replace for small targeted edits; use write_file when rewriting most of the file. "
        "Always read_file first so your content or search_text matches what is actually on disk."
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["write_file", "search_replace"],
                "description": "The file operation to perform.",
            },
            "path": {
                "type": "string",
                "description": "Path relative to the project root (e.g. 'src/main.py', 'tests/test_main.py').",
            },
            "content": {
                "type": "string",
                "description": "Complete file content for write_file. Must be non-empty.",
            },
            "search_text": {
                "type": "string",
                "description": "Exact text to find in the file (search_replace only).",
            },
            "replace_text": {
                "type": "string",
                "description": "Replacement text (search_replace only).",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences rather than just the first (search_replace only). Default false.",
            },
        },
        "required": ["operation", "path"],
    },
)


DELETE_PATH_TOOL = ToolDefinition(
    name="delete_path",
    description=(
        "Delete a file or directory (including all its contents) from the project directory. "
        "Use this to remove deprecated files, dead code, or empty directories. "
        "All paths are relative to the project root and restricted to the project directory. "
        "Cannot delete the project root itself."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file or directory to delete (e.g. 'src/legacy.py', 'old_module/').",
            },
        },
        "required": ["path"],
    },
)

RUN_SHELL_BG_TOOL = ToolDefinition(
    name="run_shell_background",
    description=(
        "Start a long-running shell command in the background and return a handle immediately. "
        "Use this for commands that stay running: dev servers, watchers, `npm start`, `python server.py`, etc. "
        "Use run_shell (not this) for short commands that finish on their own: installs, builds, tests. "
        "After starting, call get_shell_output with the handle to check startup output."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run in the background."},
        },
        "required": ["command"],
    },
)

WEB_FETCH_TOOL = ToolDefinition(
    name="fetch_webpage",
    description=(
        "Fetch a web page using a headless browser that executes JavaScript, then return its readable text content. "
        "Use this when a web_search snippet is insufficient and you need the full page — e.g. to read library docs, "
        "check a package's README, verify an API's capabilities, or inspect a GitHub repo page. "
        "Avoid fetching very large pages; prefer specific doc URLs over homepages."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full URL to fetch (must start with http:// or https://).",
            },
        },
        "required": ["url"],
    },
)

READ_PRD_TOOL = ToolDefinition(
    name="read_prd",
    description=(
        "Read the full Product Requirements Document (PRD) for this project. "
        "Call this BEFORE implementing to understand the exact requirements, rules, and constraints. "
        "Call it AGAIN after writing all files to verify your implementation is complete and correct — "
        "if anything is missing or wrong, fix it before returning your summary."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
)

GENERATE_IMAGE_TOOL = ToolDefinition(
    name="generate_image",
    description=(
        "Generate an image from a text prompt using the local ComfyUI service and save it "
        "to the project directory. Returns the relative path where the image was written. "
        "Use this to create image assets the project needs: hero images, backgrounds, icons, "
        "logos, placeholders, etc. The output_path must be relative to the project root "
        "(e.g. 'public/images/hero.png', 'assets/logo.png', 'src/assets/bg.jpg')."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Detailed description of the image to generate.",
            },
            "output_path": {
                "type": "string",
                "description": "Relative path within the project to save the image (e.g. 'public/images/hero.png').",
            },
            "negative_prompt": {
                "type": "string",
                "description": "Things to avoid in the image. Defaults to generic quality filters.",
            },
            "width": {
                "type": "integer",
                "description": "Width in pixels (default 512, max 2048). Use 1024 for SDXL models.",
            },
            "height": {
                "type": "integer",
                "description": "Height in pixels (default 512, max 2048). Use 1024 for SDXL models.",
            },
            "steps": {
                "type": "integer",
                "description": "Number of diffusion steps (default 20). More steps = higher quality but slower.",
            },
        },
        "required": ["prompt", "output_path"],
    },
)

INSPECT_FILES_TOOL = ToolDefinition(
    name="inspect_files",
    description=(
        "Read up to 10 files and return their content (truncated to 1 500 chars each) plus a "
        "`has_stubs` flag that detects TODO/FIXME/NotImplementedError markers. "
        "Use this instead of calling read_file repeatedly — all files are returned in a single "
        "tool response so you can inspect many files without burning extra rounds. "
        "Pass up to 10 file paths at a time."
    ),
    parameters={
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Relative paths (within the project) of files to inspect.",
            },
            "focus": {
                "type": "string",
                "description": "Optional: what to look for, e.g. 'API endpoints', 'auth logic', 'DB models'.",
            },
        },
        "required": ["paths"],
    },
)

GET_SHELL_OUTPUT_TOOL = ToolDefinition(
    name="get_shell_output",
    description=(
        "Get the most recent output lines from a background process started with run_shell_background. "
        "Use this to check if the server started successfully, see error messages, or monitor progress."
    ),
    parameters={
        "type": "object",
        "properties": {
            "handle": {"type": "string", "description": "The handle returned by run_shell_background."},
            "tail": {"type": "integer", "description": "Number of most recent lines to return (default 50)."},
        },
        "required": ["handle"],
    },
)

STOP_SHELL_PROCESS_TOOL = ToolDefinition(
    name="stop_shell_process",
    description=(
        "Kill a background process started with run_shell_background. "
        "Provide either the handle returned by run_shell_background OR the pid returned in the same response. "
        "Using pid is useful when you no longer have the handle (e.g. in a follow-up turn)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "handle": {"type": "string", "description": "The handle returned by run_shell_background."},
            "pid": {"type": "integer", "description": "The OS process ID returned by run_shell_background. Use this if you no longer have the handle."},
        },
    },
)

KILL_PORT_TOOL = ToolDefinition(
    name="kill_port",
    description=(
        "Kill the process currently listening on a given port number. "
        "Use this when a server start or build check fails with EADDRINUSE / 'address already in use'. "
        "For example, if starting the project server on port 3000 fails, "
        "call kill_port(port=3000) then retry. Safe to call even when nothing is listening."
    ),
    parameters={
        "type": "object",
        "properties": {
            "port": {
                "type": "integer",
                "description": "The port number to free (e.g. 3000, 8080, 5000).",
            },
        },
        "required": ["port"],
    },
)


class InferenceClientError(Exception):
    pass


def _format_tool_history(messages: list) -> str:
    """Render tool-call/result pairs as compact readable lines for the summarizer."""
    lines: list[str] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.role == "assistant" and msg.tool_calls:
            for j, tc in enumerate(msg.tool_calls):
                result_idx = i + 1 + j
                result_summary = ""
                if result_idx < len(messages) and messages[result_idx].role == "tool":
                    try:
                        res = json.loads(messages[result_idx].content)
                        if res.get("pruned"):
                            result_summary = "(already summarised)"
                        elif "content" in res:
                            result_summary = f"{res.get('total_lines', '?')} lines read"
                        elif "entries" in res:
                            result_summary = f"{len(res['entries'])} entries"
                        elif "success" in res:
                            result_summary = "ok" if res["success"] else f"FAILED: {res.get('detail') or res.get('error', '?')}"
                        elif "stdout" in res:
                            out = (res.get("stdout") or "").strip()[:80]
                            result_summary = f"exit={res.get('exit_code')} {out}"
                        elif "prd" in res:
                            result_summary = f"{len(res['prd'])} chars"
                        else:
                            result_summary = str(res)[:80]
                    except (json.JSONDecodeError, AttributeError):
                        result_summary = messages[result_idx].content[:60]
                path = tc.arguments.get("path") or tc.arguments.get("query") or tc.arguments.get("command") or ""
                op = tc.arguments.get("operation", "")
                suffix = f"({path!r}" + (f", {op}" if op else "") + f") → {result_summary}"
                lines.append(f"- {tc.name}{suffix}")
            i += 1 + len(msg.tool_calls)
        elif msg.role == "user" and msg.content.startswith("## "):
            # injected summary or nudge — skip, it's already context
            i += 1
        else:
            i += 1
    return "\n".join(lines) if lines else "(no tool calls recorded)"


class InferenceClient:
    """
    The single entry point for all LLM calls in the system.
    Handles backend routing, audit logging, JSON parsing, and tool-use loops.
    Pipeline stages never call backends directly.
    """

    def __init__(
        self,
        registry: ModelRegistry,
        drivers: dict[str, InferenceBackend],
    ) -> None:
        self._registry = registry
        self._drivers = drivers

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def call(
        self,
        stage_key: str,
        messages: list[Message],
        session: AsyncSession,
        idea_id: str,
        branch_id: str | None = None,
        stage_result_id: str | None = None,
        call_type: str = "STAGE",
        call_index: int = 0,
        overrides: dict | None = None,
        model_override: str | None = None,
    ) -> dict:
        """
        Single-turn model call: send messages, get JSON back.
        Raises InferenceClientError on backend failure or non-JSON response.
        """
        stage_cfg = self._registry.get_stage(stage_key)
        driver = self._get_driver(stage_cfg.backend)
        effective_model = model_override or stage_cfg.model

        temperature = stage_cfg.temperature
        max_tokens = stage_cfg.max_tokens
        if overrides:
            temperature = overrides.get("temperature", temperature)
            max_tokens = overrides.get("max_tokens", max_tokens)

        request = InferenceRequest(
            model=effective_model,
            messages=messages,
            format=stage_cfg.format,
            temperature=temperature,
            max_tokens=max_tokens,
            num_ctx=stage_cfg.num_ctx,
            timeout_seconds=stage_cfg.timeout_seconds,
            extra=stage_cfg.extra,
        )

        response: InferenceResponse | None = None
        error_str: str | None = None

        logger.info("call  stage=%-20s model=%s", stage_key, effective_model)
        try:
            response = await driver.complete(request)
        except InferenceBackendError as e:
            error_str = str(e)
            logger.error("call  stage=%-20s FAILED: %s", stage_key, e)
            raise InferenceClientError(
                f"Backend '{stage_cfg.backend}' failed for stage '{stage_key}': {e}"
            ) from e
        finally:
            await self._log_call(
                session=session,
                idea_id=idea_id,
                branch_id=branch_id,
                stage_result_id=stage_result_id,
                call_type=call_type,
                call_index=call_index,
                model_name=effective_model,
                backend=stage_cfg.backend,
                request=request,
                response=response,
                error=error_str,
                stage_key=stage_key,
            )

        try:
            parsed = json.loads(_strip_markdown_json(response.content))
        except json.JSONDecodeError as e:
            raise InferenceClientError(
                f"Stage '{stage_key}' returned non-JSON content: {response.content[:200]}"
            ) from e
        if isinstance(parsed, list):
            if len(parsed) == 1 and isinstance(parsed[0], dict):
                logger.warning("call  stage=%-20s model returned list([dict]) — unwrapping", stage_key)
                parsed = parsed[0]
            else:
                raise InferenceClientError(
                    f"Stage '{stage_key}' returned a JSON array instead of an object: {response.content[:200]}"
                )
        return parsed

    async def stream_text(
        self,
        stage_key: str,
        messages: list[Message],
    ):
        """
        Streaming text response — yields string chunks as they arrive from the model.
        Does not enforce JSON. Does not log to audit trail (caller is responsible for
        persisting the final content).
        """
        stage_cfg = self._registry.get_stage(stage_key)
        driver = self._get_driver(stage_cfg.backend)

        request = InferenceRequest(
            model=stage_cfg.model,
            messages=messages,
            format="",  # free-form; OllamaDriver omits format field
            temperature=stage_cfg.temperature,
            max_tokens=stage_cfg.max_tokens,
            num_ctx=stage_cfg.num_ctx,
            extra=stage_cfg.extra,
        )

        logger.info("stream_text  stage=%-20s model=%s", stage_key, stage_cfg.model)
        async for chunk in driver.stream_complete(request):
            yield chunk

    async def call_text(
        self,
        stage_key: str,
        messages: list[Message],
        session: AsyncSession,
        idea_id: str,
        branch_id: str | None = None,
        call_type: str = "PHASE2",
        call_index: int = 0,
        model_override: str | None = None,
    ) -> str:
        """
        Free-form text call — returns plain text instead of parsed JSON.
        Used for Phase 2 conversational responses (markdown, not structured output).
        """
        stage_cfg = self._registry.get_stage(stage_key)
        driver = self._get_driver(stage_cfg.backend)
        effective_model = model_override or stage_cfg.model

        request = InferenceRequest(
            model=effective_model,
            messages=messages,
            format="",  # no JSON enforcement — driver skips format field
            temperature=stage_cfg.temperature,
            max_tokens=stage_cfg.max_tokens,
            num_ctx=stage_cfg.num_ctx,
        )

        response: InferenceResponse | None = None
        error_str: str | None = None

        logger.info("call_text  stage=%-20s model=%s", stage_key, effective_model)
        try:
            response = await driver.complete(request)
        except InferenceBackendError as e:
            error_str = str(e)
            logger.error("call_text  stage=%-20s FAILED: %s", stage_key, e)
            raise InferenceClientError(
                f"Backend '{stage_cfg.backend}' failed for stage '{stage_key}': {e}"
            ) from e
        finally:
            await self._log_call(
                session=session,
                idea_id=idea_id,
                branch_id=branch_id,
                stage_result_id=None,
                call_type=call_type,
                call_index=call_index,
                model_name=stage_cfg.model,
                backend=stage_cfg.backend,
                request=request,
                response=response,
                error=error_str,
                stage_key=stage_key,
            )

        return response.content.strip() if response.content else ""

    async def call_with_tools(
        self,
        stage_key: str,
        messages: list[Message],
        session: AsyncSession,
        idea_id: str,
        branch_id: str | None = None,
        stage_result_id: str | None = None,
        call_index: int = 0,
        max_tool_rounds: int | None = 8,
        allowed_file_dir: str | None = None,
        explore_only: bool = False,
        on_tool_result=None,   # Optional[Callable[[str, dict], Awaitable[None]]]
        on_text_response=None, # Optional[Callable[[str], Awaitable[None]]] — fires when model returns text with no tool calls
        return_json: bool = True,
        extra_tools: "list[ToolDefinition] | None" = None,
        custom_tool_handlers: "dict | None" = None,  # {name: async callable(args) -> dict}
        model_override: str | None = None,
        agent_id: str | None = None,
    ) -> dict | str:
        """
        Multi-turn tool-use call. The model may invoke `run_python` zero or more times
        before returning a final JSON answer.

        Flow per round:
          1. Call model with RUN_PYTHON_TOOL exposed.
          2. If response contains tool_calls → execute each script, log it, append
             tool result message, repeat.
          3. If response contains plain content → parse JSON and return.

        Each model call and each script execution is logged to the audit trail.
        """
        from app.config import settings
        from app.tools.script_runner import run_script
        from app.tools.web_search import web_search as web_search_fn

        stage_cfg = self._registry.get_stage(stage_key)
        driver = self._get_driver(stage_cfg.backend)

        # Working copy of the conversation; we'll extend it with tool results
        working_messages = list(messages)
        current_call_index = call_index

        if not stage_cfg.supports_tools:
            logger.debug("tools stage=%-20s model=%s does not support tools — using plain call", stage_key, stage_cfg.model)
            return await self.call(
                stage_key=stage_key,
                messages=messages,
                session=session,
                idea_id=idea_id,
                branch_id=branch_id,
                stage_result_id=stage_result_id,
                call_index=call_index,
            )

        # web_search + fetch_webpage are always available
        # file exploration tools (list/read/grep) are available whenever a project dir is provided
        # file_edit and run_shell are available when allowed_file_dir is set and NOT explore_only
        available_tools: list[ToolDefinition] = [RUN_PYTHON_TOOL, WEB_SEARCH_TOOL, WEB_FETCH_TOOL]
        if allowed_file_dir:
            available_tools += [LIST_FILES_TOOL, READ_FILE_TOOL, GREP_FILES_TOOL]
            if not explore_only:
                available_tools += [FILE_EDIT_TOOL, DELETE_PATH_TOOL, SHELL_TOOL, RUN_SHELL_BG_TOOL, GET_SHELL_OUTPUT_TOOL, STOP_SHELL_PROCESS_TOOL, KILL_PORT_TOOL]
        if extra_tools:
            available_tools += extra_tools
            # If inspect_files is provided, remove read_file so the model is forced
            # to batch file reads via inspect_files (returns content in one round)
            # instead of calling read_file one file per round.
            if any(t.name == "inspect_files" for t in extra_tools):
                available_tools = [t for t in available_tools if t.name != "read_file"]

        effective_model = model_override or stage_cfg.model
        _agent_suffix = f"  agent={agent_id}" if agent_id else ""
        logger.info("tools stage=%-20s model=%s  tools=%s  round_limit=%s%s",
                    stage_key, effective_model, [t.name for t in available_tools],
                    max_tool_rounds if max_tool_rounds is not None else "unlimited", _agent_suffix)
        round_num = 0
        _stall_sig: frozenset | None = None
        _stall_count = 0
        _total_stall_events = 0
        _tool_call_counts: dict[str, int] = {}
        while max_tool_rounds is None or round_num <= max_tool_rounds:
            logger.info("tools stage=%-20s round=%d%s", stage_key, round_num, _agent_suffix)
            if round_num > 0:
                from app.tools.context_reducer import prune_stale_reads
                working_messages = prune_stale_reads(working_messages)
            request = InferenceRequest(
                model=effective_model,
                messages=working_messages,
                format=stage_cfg.format,
                temperature=stage_cfg.temperature,
                max_tokens=stage_cfg.max_tokens,
                num_ctx=stage_cfg.num_ctx,
                tools=available_tools,
                timeout_seconds=stage_cfg.timeout_seconds,
                extra=stage_cfg.extra,
            )

            response: InferenceResponse | None = None
            error_str: str | None = None

            try:
                response = await driver.complete(request)
            except InferenceBackendError as e:
                error_str = str(e)
                await self._log_call(
                    session=session, idea_id=idea_id, branch_id=branch_id,
                    stage_result_id=stage_result_id, call_type="STAGE",
                    call_index=current_call_index, model_name=effective_model,
                    backend=stage_cfg.backend, request=request,
                    response=None, error=error_str,
                    stage_key=stage_key,
                )
                # If this is the first round (no tool results appended yet) the model
                # likely doesn't support tool-calling.  Fall back to a plain call so
                # the pipeline keeps running without script execution.
                if round_num == 0:
                    logger.warning(
                        "tools stage=%-20s rejected by backend (%s) — falling back to plain call",
                        stage_key, e,
                    )
                    if not return_json:
                        return await self.call_text(
                            stage_key=stage_key,
                            messages=messages,
                            session=session,
                            idea_id=idea_id,
                            branch_id=branch_id,
                            call_index=current_call_index,
                        )
                    return await self.call(
                        stage_key=stage_key,
                        messages=messages,
                        session=session,
                        idea_id=idea_id,
                        branch_id=branch_id,
                        stage_result_id=stage_result_id,
                        call_index=current_call_index,
                        model_override=effective_model,
                    )
                raise InferenceClientError(
                    f"Backend '{stage_cfg.backend}' failed for stage '{stage_key}': {e}"
                ) from e

            await self._log_call(
                session=session, idea_id=idea_id, branch_id=branch_id,
                stage_result_id=stage_result_id, call_type="STAGE",
                call_index=current_call_index, model_name=effective_model,
                backend=stage_cfg.backend, request=request,
                response=response, error=None,
                stage_key=stage_key,
            )
            current_call_index += 1

            # Context compression: when the model reports it consumed ≥75% of
            # num_ctx, summarize old tool rounds before appending this round's
            # results so the next model call has headroom. Only active for
            # agentic (file-writing) stages — allowed_file_dir is the signal.
            if allowed_file_dir:
                _ctx_used = response.tokens_prompt
                if _ctx_used is None:
                    # Backend doesn't report tokens — estimate from char length
                    _ctx_used = sum(len(m.content or "") for m in working_messages) // 3
                if _ctx_used >= int(stage_cfg.num_ctx * 0.75):
                    logger.info(
                        "tools stage=%-20s context at %d/%d tokens (%.0f%%) — compressing%s",
                        stage_key, _ctx_used, stage_cfg.num_ctx,
                        _ctx_used / stage_cfg.num_ctx * 100,
                        f"  agent={agent_id}" if agent_id else "",
                    )
                    working_messages = await self._compress_context(
                        working_messages, stage_key, agent_id
                    )

            # Some local models emit tool calls as plain-text JSON instead of
            # using the structured tool-call API. Detect and promote them so
            # the normal dispatch branch handles them without a second LLM call.
            if not response.tool_calls and response.content:
                _raw_content = _strip_markdown_json(response.content.strip())
                try:
                    _maybe_tc = json.loads(_raw_content)
                except json.JSONDecodeError:
                    # Try NDJSON: multiple JSON objects on separate lines
                    _maybe_tc = None
                    _lines = [ln.strip() for ln in _raw_content.splitlines() if ln.strip().startswith("{")]
                    if len(_lines) >= 2:
                        try:
                            _parsed_lines = [json.loads(ln) for ln in _lines]
                            if all(isinstance(o, dict) and "name" in o and "arguments" in o for o in _parsed_lines):
                                _maybe_tc = _parsed_lines
                        except json.JSONDecodeError:
                            pass
                if _maybe_tc is not None:
                    _tc_list: list[dict] = []
                    if isinstance(_maybe_tc, dict) and "name" in _maybe_tc and "arguments" in _maybe_tc:
                        _tc_list = [_maybe_tc]
                    elif isinstance(_maybe_tc, list) and all(
                        isinstance(t, dict) and "name" in t and "arguments" in t for t in _maybe_tc
                    ):
                        _tc_list = _maybe_tc
                    if _tc_list:
                        known_names = {t.name for t in available_tools}
                        _valid_tc = [t for t in _tc_list if t["name"] in known_names]
                        if _valid_tc:
                            logger.warning(
                                "tools stage=%-20s model emitted %d tool call(s) as plain text — dispatching: %s",
                                stage_key, len(_valid_tc), [t["name"] for t in _valid_tc],
                            )
                            response.tool_calls = [
                                ToolCall(name=t["name"], arguments=t.get("arguments") or {})
                                for t in _valid_tc
                            ]
                            response.content = ""

            # Model called a tool — execute each, append results, loop
            if response.tool_calls:
                # Stall detection: if the model keeps calling the same tool(s) with the same
                # arguments round after round, it's stuck. Inject a nudge after 3 repeats.
                _round_sig = frozenset(
                    (tc.name, json.dumps(tc.arguments, sort_keys=True))
                    for tc in response.tool_calls
                )
                if _round_sig == _stall_sig:
                    _stall_count += 1
                else:
                    _stall_sig = _round_sig
                    _stall_count = 1
                if _stall_count >= 3:
                    _stalled_tools = ", ".join(tc.name for tc in response.tool_calls)
                    _total_stall_events += 1
                    logger.warning(
                        "tools stage=%-20s stall: same call (%s) repeated %d times — nudging (stall_event=%d)%s",
                        stage_key, _stalled_tools, _stall_count, _total_stall_events, _agent_suffix,
                    )
                    if _total_stall_events >= 3:
                        logger.error(
                            "tools stage=%-20s stall limit reached (%d events) — aborting loop%s",
                            stage_key, _total_stall_events, _agent_suffix,
                        )
                        from app import telemetry as _tel_tc
                        _tel_tc.set_tool_counts(_tool_call_counts)
                        raise InferenceClientError(
                            f"Stage '{stage_key}' stalled: `{_stalled_tools}` repeated with identical "
                            f"arguments across {_total_stall_events} stall events — aborting"
                        )
                    _write_tools = {"file_edit", "write_file", "delete_path"}
                    _is_write_stall = all(tc.name in _write_tools for tc in response.tool_calls)
                    if _is_write_stall:
                        _nudge_msg = (
                            f"You have called `{_stalled_tools}` with identical arguments "
                            f"{_stall_count} times in a row. Each call succeeded — the file is already saved. "
                            "Do NOT call `file_edit` again for this file.\n\n"
                            "If your task is complete, output your JSON summary now:\n"
                            '{"summary": "...", "files_written": [...], "commands_run": [...], "success": true, "blocker": null}\n\n'
                            "If you still have other files to write, write them now. Do not re-write files you have already written."
                        )
                    else:
                        _nudge_msg = (
                            f"You have called `{_stalled_tools}` with identical arguments "
                            f"{_stall_count} times in a row and received the same result each time. "
                            "This loop is not making progress.\n\n"
                            "STOP all exploration now. Call `file_edit` to write your assigned files "
                            "immediately. If you are unsure of the exact content, write your best "
                            "implementation — do not call any listing or reading tools again."
                        )
                    working_messages.append(Message(role="user", content=_nudge_msg))
                    _stall_sig = None
                    _stall_count = 0

                # Append the assistant's tool-call turn to the conversation
                working_messages.append(
                    Message(role="assistant", content="", tool_calls=response.tool_calls)
                )

                for tc in response.tool_calls:
                    _tool_call_counts[tc.name] = _tool_call_counts.get(tc.name, 0) + 1
                    if tc.name == "run_python":
                        script = tc.arguments.get("script", "")
                        script_result = await run_script(
                            script,
                            timeout_seconds=settings.script_runner_timeout_seconds,
                            max_output_bytes=settings.script_runner_max_output_kb * 1024,
                        )
                        result_dict = {
                            "stdout": script_result.stdout,
                            "stderr": script_result.stderr,
                            "exit_code": script_result.exit_code,
                            "timed_out": script_result.timed_out,
                        }
                        _script_hint = " ".join(script.split())[:80]
                        _py_status = "TIMEOUT" if script_result.timed_out else f"exit={script_result.exit_code}"
                        logger.info("tools stage=%-20s run_python %s dur=%dms | %s%s",
                                    stage_key, _py_status, script_result.duration_ms, _script_hint, _agent_suffix)
                        await self._log_script_execution(
                            session=session, idea_id=idea_id, branch_id=branch_id,
                            stage_result_id=stage_result_id, call_index=current_call_index,
                            script=script, result=result_dict,
                            duration_ms=script_result.duration_ms,
                        )
                        current_call_index += 1
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif tc.name == "web_search":
                        query = tc.arguments.get("query", "")
                        logger.info("tools stage=%-20s web_search query=%r", stage_key, query)
                        search_result = await web_search_fn(
                            query=query,
                            tavily_api_key=settings.tavily_api_key,
                        )
                        result_dict = {
                            "query": search_result.query,
                            "results": [
                                {"title": h.title, "url": h.url, "snippet": h.snippet}
                                for h in search_result.results
                            ],
                        }
                        if search_result.error:
                            result_dict["error"] = search_result.error
                        if on_tool_result:
                            await on_tool_result("web_search", {
                                "query": query,
                                "result_count": len(result_dict.get("results", [])),
                            })
                        await self._log_web_search(
                            session=session, idea_id=idea_id, branch_id=branch_id,
                            stage_result_id=stage_result_id, call_index=current_call_index,
                            query=query, result=result_dict,
                            duration_ms=search_result.duration_ms,
                        )
                        current_call_index += 1
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif tc.name == "fetch_webpage":
                        url = tc.arguments.get("url", "").strip()
                        logger.info("tools stage=%-20s fetch_webpage url=%r", stage_key, url)
                        from app.tools.web_fetch import fetch_webpage as fetch_webpage_fn
                        fetch_result = await fetch_webpage_fn(
                            url=url,
                            timeout_seconds=25,
                        )
                        result_dict = {
                            "url": fetch_result.url,
                            "title": fetch_result.title,
                            "content": fetch_result.content,
                            "truncated": fetch_result.truncated,
                        }
                        if fetch_result.error:
                            result_dict["error"] = fetch_result.error
                        if on_tool_result:
                            await on_tool_result("fetch_webpage", {
                                "url": url,
                                "title": fetch_result.title,
                                "content_length": len(fetch_result.content),
                                "truncated": fetch_result.truncated,
                                "error": fetch_result.error,
                            })
                        current_call_index += 1
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif tc.name == "file_edit":
                        if not allowed_file_dir or explore_only:
                            result_dict = {"success": False, "error": "file_edit tool not available in this context"}
                        elif (
                            tc.arguments.get("operation") == "write_file"
                            and _pathlib.Path(tc.arguments.get("path", "")).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
                            and settings.comfyui_base_url
                        ):
                            # Agent is trying to write text content to a binary image path.
                            # Redirect to generate_image instead of silently writing garbage.
                            _img_path = tc.arguments.get("path", "")
                            result_dict = {
                                "success": False,
                                "error": (
                                    f"Cannot write text content to image file '{_img_path}' via file_edit. "
                                    "Use the generate_image tool instead: "
                                    f"generate_image(prompt='...', output_path='{_img_path}'). "
                                    "generate_image calls the local ComfyUI service and saves a real PNG."
                                ),
                            }
                            logger.info(
                                "tools stage=%-20s file_edit blocked image write path=%r — redirected to generate_image",
                                stage_key, _img_path,
                            )
                        else:
                            from app.tools.file_editor import edit_file
                            import time as _time
                            _t0 = _time.monotonic()
                            display_path = normalize_project_relative_path(
                                allowed_file_dir,
                                tc.arguments.get("path", ""),
                            )
                            edit_result = await edit_file(
                                operation=tc.arguments.get("operation", ""),
                                path=display_path,
                                allowed_base_dir=allowed_file_dir,
                                content=tc.arguments.get("content", ""),
                                search_text=tc.arguments.get("search_text", ""),
                                replace_text=tc.arguments.get("replace_text", ""),
                                after_line=tc.arguments.get("after_line", -1),
                                replace_all=tc.arguments.get("replace_all", False),
                            )
                            _duration_ms = int((_time.monotonic() - _t0) * 1000)
                            result_dict = {
                                "success": edit_result.success,
                                "operation": edit_result.operation,
                                "path": display_path,
                                "detail": edit_result.detail,
                            }
                            logger.info(
                                "tools stage=%-20s file_edit op=%s path=%r ok=%s",
                                stage_key, edit_result.operation, edit_result.path, edit_result.success,
                            )
                            await self._log_file_edit(
                                session=session, idea_id=idea_id, branch_id=branch_id,
                                stage_result_id=stage_result_id, call_index=current_call_index,
                                operation=tc.arguments.get("operation", ""),
                                path=display_path,
                                result=result_dict,
                                duration_ms=_duration_ms,
                            )
                            current_call_index += 1
                            if on_tool_result:
                                content_bytes = len((tc.arguments.get("content") or "").encode("utf-8"))
                                await on_tool_result("file_edit", {
                                    "path": display_path,
                                    "operation": edit_result.operation,
                                    "success": edit_result.success,
                                    "size_bytes": content_bytes,
                                    "detail": edit_result.detail,
                                })
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif tc.name == "delete_path":
                        if not allowed_file_dir or explore_only:
                            result_dict = {"success": False, "error": "delete_path tool not available in this context"}
                        else:
                            from app.tools.file_editor import delete_path
                            display_path = normalize_project_relative_path(
                                allowed_file_dir,
                                tc.arguments.get("path", ""),
                            )
                            del_result = await delete_path(
                                path=display_path,
                                allowed_base_dir=allowed_file_dir,
                            )
                            result_dict = {
                                "success": del_result.success,
                                "path": display_path,
                                "detail": del_result.detail,
                            }
                            logger.info(
                                "tools stage=%-20s delete_path path=%r ok=%s",
                                stage_key, display_path, del_result.success,
                            )
                            if on_tool_result:
                                await on_tool_result("delete_path", {
                                    "path": display_path,
                                    "success": del_result.success,
                                    "detail": del_result.detail,
                                })
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif tc.name == "run_shell":
                        if not allowed_file_dir or explore_only:
                            result_dict = {"success": False, "error": "run_shell tool not available in this context"}
                        else:
                            from app.tools.shell_runner import run_shell_command
                            command = tc.arguments.get("command", "")
                            logger.info("tools stage=%-20s run_shell command=%r", stage_key, command)
                            shell_result = await run_shell_command(
                                command=command,
                                working_dir=allowed_file_dir,
                                timeout_seconds=settings.shell_runner_timeout_seconds,
                                max_output_bytes=settings.shell_runner_max_output_kb * 1024,
                            )
                            result_dict = {
                                "stdout": shell_result.stdout,
                                "stderr": shell_result.stderr,
                                "exit_code": shell_result.exit_code,
                                "timed_out": shell_result.timed_out,
                                "duration_ms": shell_result.duration_ms,
                            }
                            await self._log_shell_command(
                                session=session, idea_id=idea_id, branch_id=branch_id,
                                stage_result_id=stage_result_id, call_index=current_call_index,
                                command=command, result=result_dict,
                                duration_ms=shell_result.duration_ms,
                            )
                            current_call_index += 1
                            if on_tool_result:
                                await on_tool_result("run_shell", {
                                    "command": command,
                                    "exit_code": shell_result.exit_code,
                                    "stdout": shell_result.stdout,
                                    "stderr": shell_result.stderr,
                                    "timed_out": shell_result.timed_out,
                                    "duration_ms": shell_result.duration_ms,
                                })
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif tc.name == "run_shell_background":
                        if not allowed_file_dir or explore_only:
                            result_dict = {"error": "run_shell_background not available in this context"}
                        else:
                            from app.tools.shell_runner import background_process_manager
                            command = tc.arguments.get("command", "")
                            logger.info("tools stage=%-20s run_shell_background command=%r", stage_key, command)
                            handle, pid, error = await background_process_manager.start(command, allowed_file_dir)
                            if error:
                                result_dict = {"error": error}
                            else:
                                await asyncio.sleep(1.5)  # brief pause so startup lines can accumulate
                                output = await background_process_manager.get_output(handle, tail=30)
                                result_dict = {"handle": handle, "pid": pid, "started": True, **output}
                                if on_tool_result:
                                    await on_tool_result("run_shell_background", {
                                        "command": command,
                                        "handle": handle,
                                        "pid": pid,
                                        "lines": output.get("lines", []),
                                        "is_running": output.get("is_running", True),
                                    })
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif tc.name == "get_shell_output":
                        if not allowed_file_dir or explore_only:
                            result_dict = {"error": "get_shell_output not available in this context"}
                        else:
                            from app.tools.shell_runner import background_process_manager
                            handle = tc.arguments.get("handle", "")
                            tail = int(tc.arguments.get("tail") or 50)
                            result_dict = await background_process_manager.get_output(handle, tail)
                            logger.info("tools stage=%-20s get_shell_output handle=%r running=%s",
                                        stage_key, handle, result_dict.get("is_running"))
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif tc.name == "stop_shell_process":
                        if not allowed_file_dir or explore_only:
                            result_dict = {"error": "stop_shell_process not available in this context"}
                        else:
                            from app.tools.shell_runner import background_process_manager
                            handle = tc.arguments.get("handle") or None
                            pid = tc.arguments.get("pid")
                            pid = int(pid) if pid is not None else None
                            result_dict = await background_process_manager.stop(handle=handle, pid=pid)
                            logger.info("tools stage=%-20s stop_shell_process handle=%r pid=%r", stage_key, handle, pid)
                            if on_tool_result:
                                label = handle or f"pid:{pid}"
                                await on_tool_result("shell_stop", {
                                    "handle": label,
                                    "pid": result_dict.get("pid", pid),
                                    "stopped": result_dict.get("stopped", False),
                                    "exit_code": result_dict.get("exit_code"),
                                    "message": result_dict.get("message", result_dict.get("error", "")),
                                })
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif tc.name == "kill_port":
                        if not allowed_file_dir or explore_only:
                            result_dict = {"error": "kill_port not available in this context"}
                        else:
                            from app.tools.shell_runner import kill_port_process
                            port = int(tc.arguments.get("port", 0))
                            result_dict = kill_port_process(port)
                            logger.info("tools stage=%-20s kill_port port=%d killed=%s pids=%s",
                                        stage_key, port, result_dict.get("killed"), result_dict.get("pids"))
                            if on_tool_result:
                                await on_tool_result("kill_port", result_dict)
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif tc.name == "list_files":
                        if not allowed_file_dir:
                            result_dict = {"error": "list_files not available in this context"}
                        else:
                            import os as _os
                            from pathlib import Path as _Path
                            _base = _Path(allowed_file_dir).resolve()
                            _rel = normalize_project_relative_path(
                                allowed_file_dir,
                                tc.arguments.get("path") or "",
                            )
                            _target = (_base / _rel).resolve() if _rel and _rel != "." else _base
                            _SKIP = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".next"}
                            if not str(_target).startswith(str(_base)):
                                result_dict = {"error": "path outside project directory"}
                            elif not _target.exists():
                                result_dict = {"error": f"path not found: {_rel or '.'}"}
                            else:
                                entries = []
                                try:
                                    for entry in sorted(_target.iterdir()):
                                        if entry.name in _SKIP:
                                            continue
                                        entries.append({
                                            "name": entry.name,
                                            "type": "dir" if entry.is_dir() else "file",
                                            "path": str(entry.relative_to(_base)).replace("\\", "/"),
                                        })
                                except Exception as _e:
                                    entries = []
                                result_dict = {"path": _rel or ".", "entries": entries}
                            logger.info("tools stage=%-20s list_files path=%r → %d entries%s",
                                        stage_key, _rel or ".", len(result_dict.get("entries", [])), _agent_suffix)
                            if on_tool_result:
                                await on_tool_result("list_files", {
                                    "path": _rel or ".",
                                    "entry_count": len(result_dict.get("entries", [])),
                                })
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif tc.name == "read_file":
                        if not allowed_file_dir:
                            result_dict = {"error": "read_file not available in this context"}
                        else:
                            from pathlib import Path as _Path
                            _CHUNK = 200  # default lines per read
                            _base = _Path(allowed_file_dir).resolve()
                            _rel = normalize_project_relative_path(
                                allowed_file_dir,
                                tc.arguments.get("path") or "",
                            )
                            _full = (_base / _rel).resolve()
                            if not str(_full).startswith(str(_base)):
                                result_dict = {"error": "path outside project directory"}
                            elif not _full.exists():
                                result_dict = {"error": f"file not found: {_rel}"}
                            elif not _full.is_file():
                                result_dict = {"error": f"not a file: {_rel}"}
                            else:
                                try:
                                    raw_bytes = _full.read_bytes()
                                    if b"\x00" in raw_bytes[:512]:
                                        result_dict = {"error": "binary file — cannot read as text"}
                                    else:
                                        all_lines = raw_bytes.decode("utf-8", errors="replace").splitlines(keepends=True)
                                        total_lines = len(all_lines)
                                        _arg_start = tc.arguments.get("start_line")
                                        _arg_end = tc.arguments.get("end_line")
                                        start = int(_arg_start) if _arg_start is not None else 0
                                        end = int(_arg_end) if _arg_end is not None else min(start + _CHUNK, total_lines)
                                        start = max(0, min(start, total_lines))
                                        end = max(start, min(end, total_lines))
                                        chunk = all_lines[start:end]
                                        result_dict = {
                                            "path": _rel,
                                            "content": "".join(chunk),
                                            "start_line": start,
                                            "end_line": end,
                                            "total_lines": total_lines,
                                            "size_bytes": len(raw_bytes),
                                        }
                                except Exception as _e:
                                    result_dict = {"error": f"failed to read: {_e}"}
                            _range_str = ""
                            if "start_line" in result_dict or "end_line" in result_dict:
                                _range_str = f" lines={result_dict.get('start_line', 0)}-{result_dict.get('end_line', '?')}/{result_dict.get('total_lines', '?')}"
                            logger.info("tools stage=%-20s read_file path=%r%s%s", stage_key, _rel, _range_str, _agent_suffix)
                            if on_tool_result:
                                await on_tool_result("read_file", {"path": _rel})
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif tc.name == "grep_files":
                        if not allowed_file_dir:
                            result_dict = {"error": "grep_files not available in this context"}
                        else:
                            import fnmatch as _fnmatch
                            from pathlib import Path as _Path
                            _base = _Path(allowed_file_dir).resolve()
                            _pattern = tc.arguments.get("pattern") or ""
                            _search_dir = normalize_project_relative_path(
                                allowed_file_dir,
                                tc.arguments.get("path") or "",
                            )
                            _glob = (tc.arguments.get("file_glob") or "").strip()
                            _target = (_base / _search_dir).resolve() if _search_dir and _search_dir != "." else _base
                            _SKIP = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".next"}
                            _MAX_RESULTS = 40
                            matches = []
                            if not _pattern:
                                result_dict = {"error": "pattern is required"}
                            elif not str(_target).startswith(str(_base)):
                                result_dict = {"error": "path outside project directory"}
                            else:
                                try:
                                    for _p in sorted(_target.rglob("*")):
                                        if not _p.is_file():
                                            continue
                                        if any(part in _SKIP for part in _p.parts):
                                            continue
                                        if _glob and not _fnmatch.fnmatch(_p.name, _glob):
                                            continue
                                        if len(matches) >= _MAX_RESULTS:
                                            break
                                        try:
                                            raw_bytes = _p.read_bytes()
                                            if b"\x00" in raw_bytes[:512]:
                                                continue
                                            text = raw_bytes.decode("utf-8", errors="replace")
                                        except Exception:
                                            continue
                                        for ln, line in enumerate(text.splitlines(), 1):
                                            if _pattern in line:
                                                matches.append({
                                                    "file": str(_p.relative_to(_base)).replace("\\", "/"),
                                                    "line": ln,
                                                    "text": line.strip(),
                                                })
                                                if len(matches) >= _MAX_RESULTS:
                                                    break
                                    result_dict = {
                                        "pattern": _pattern,
                                        "matches": matches,
                                        "truncated": len(matches) >= _MAX_RESULTS,
                                    }
                                except Exception as _e:
                                    result_dict = {"error": f"grep failed: {_e}"}
                            logger.info("tools stage=%-20s grep_files pattern=%r → %d matches%s",
                                        stage_key, _pattern, len(result_dict.get("matches", [])), _agent_suffix)
                            if on_tool_result:
                                await on_tool_result("grep_files", {
                                    "pattern": _pattern,
                                    "match_count": len(result_dict.get("matches", [])),
                                })
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif tc.name == "generate_image":
                        if not allowed_file_dir or explore_only:
                            result_dict = {"success": False, "error": "generate_image not available in this context"}
                        elif not settings.comfyui_base_url:
                            result_dict = {"success": False, "error": "ComfyUI is not configured (set COMFYUI_BASE_URL in .env)"}
                        else:
                            from app.tools.image_generator import generate_image as _generate_image
                            # Accept both 'output_path' and 'path' — local models often use the shorter name
                            _out_path = (
                                tc.arguments.get("output_path")
                                or tc.arguments.get("path")
                                or ""
                            ).strip()
                            logger.info(
                                "tools stage=%-20s generate_image input_path=%r prompt=%r",
                                stage_key, _out_path, (tc.arguments.get("prompt") or "")[:80],
                            )
                            if not _out_path:
                                result_dict = {
                                    "success": False,
                                    "error": (
                                        "Missing required parameter 'output_path'. "
                                        "Call generate_image with output_path set to a relative file path, "
                                        "e.g. output_path='public/images/hero.png'."
                                    ),
                                }
                            else:
                                _img_result = await _generate_image(
                                    prompt=tc.arguments.get("prompt", ""),
                                    output_path=_out_path,
                                    allowed_base_dir=allowed_file_dir,
                                    base_url=settings.comfyui_base_url,
                                    model_name=settings.comfyui_model,
                                    negative_prompt=tc.arguments.get("negative_prompt", ""),
                                    width=int(tc.arguments.get("width") or 512),
                                    height=int(tc.arguments.get("height") or 512),
                                    steps=int(tc.arguments.get("steps") or 20),
                                )
                                result_dict = {
                                    "success": _img_result.error is None,
                                    "path": _img_result.path,
                                    "backend": _img_result.backend,
                                    "width": _img_result.width,
                                    "height": _img_result.height,
                                    "duration_ms": _img_result.duration_ms,
                                }
                                if _img_result.error:
                                    result_dict["error"] = _img_result.error
                                logger.info(
                                    "tools stage=%-20s generate_image saved=%r ok=%s duration_ms=%d",
                                    stage_key, _img_result.path, _img_result.error is None, _img_result.duration_ms,
                                )
                                if on_tool_result:
                                    await on_tool_result("generate_image", result_dict)
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif custom_tool_handlers and tc.name in custom_tool_handlers:
                        try:
                            result_dict = await custom_tool_handlers[tc.name](tc.arguments)
                        except Exception as _e:
                            logger.error("tools stage=%-20s custom handler %s failed: %s", stage_key, tc.name, _e)
                            result_dict = {"error": str(_e)}
                        if on_tool_result:
                            await on_tool_result(tc.name, {"tool": tc.name, **result_dict})
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    else:
                        logger.warning("tools stage=%-20s unknown tool call: %s", stage_key, tc.name)
                        working_messages.append(Message(role="tool", content=json.dumps({"error": f"Unknown tool: {tc.name}"})))

                round_num += 1
                continue  # next round

            # No tool calls — model returned its final answer
            from app import telemetry as _tel_tc
            _tel_tc.set_tool_counts(_tool_call_counts)
            content = response.content.strip() if response.content else ""
            _preview = " ".join(content.split())[:120]
            logger.info("tools stage=%-20s round=%d → no tool calls: %r%s", stage_key, round_num, _preview, _agent_suffix)
            if on_text_response and content:
                await on_text_response(content)

            if not return_json:
                return content

            # Some models return empty content after tool rounds instead of the
            # final answer.  Inject an explicit nudge and do one plain call to
            # get the JSON response.
            if not content:
                logger.warning(
                    "tools stage=%-20s empty content after round %d — nudging for final answer",
                    stage_key, round_num,
                )
                if not return_json:
                    return ""
                working_messages.append(Message(
                    role="user",
                    content="Please provide your final answer now as a JSON object only, with no additional text or markdown.",
                ))
                try:
                    return await self.call(
                        stage_key=stage_key,
                        messages=working_messages,
                        session=session,
                        idea_id=idea_id,
                        branch_id=branch_id,
                        stage_result_id=stage_result_id,
                        call_index=current_call_index,
                    )
                except InferenceClientError:
                    logger.warning("tools stage=%-20s empty-content nudge also failed — returning empty", stage_key)
                    return {"message": "", "files": [], "commands": []}

            try:
                parsed = json.loads(_strip_markdown_json(content))
            except json.JSONDecodeError as e:
                logger.error(
                    "tools stage=%-20s non-JSON response after tool rounds: %s",
                    stage_key, content[:300],
                )
                if return_json:
                    logger.warning(
                        "tools stage=%-20s nudging prose final answer into JSON",
                        stage_key,
                    )
                    working_messages.append(Message(
                        role="assistant",
                        content=content,
                    ))
                    working_messages.append(Message(
                        role="user",
                        content=(
                            "Convert your previous answer into a JSON object only, with no prose and no markdown. "
                            "Use exactly this schema: "
                            '{"message": "brief summary", "files": [{"path": "relative/path", '
                            '"description": "specific change required"}], "commands": []}. '
                            "If no files need to change, return "
                            '{"message": "No files need to change.", "files": [], "commands": []}.'
                        ),
                    ))
                    try:
                        return await self.call(
                            stage_key=stage_key,
                            messages=working_messages,
                            session=session,
                            idea_id=idea_id,
                            branch_id=branch_id,
                            stage_result_id=stage_result_id,
                            call_index=current_call_index,
                        )
                    except InferenceClientError:
                        # Nudge also failed — return the prose as the message so
                        # the iteration completes gracefully rather than crashing.
                        logger.warning(
                            "tools stage=%-20s nudge also failed — returning prose as message",
                            stage_key,
                        )
                        return {"message": content, "files": [], "commands": []}
                raise InferenceClientError(
                    f"Stage '{stage_key}' returned non-JSON after tool rounds: "
                    f"{content[:200]}"
                ) from e
            if isinstance(parsed, list):
                if len(parsed) == 1 and isinstance(parsed[0], dict):
                    logger.warning("tools stage=%-20s model returned list([dict]) — unwrapping", stage_key)
                    parsed = parsed[0]
                else:
                    raise InferenceClientError(
                        f"Stage '{stage_key}' returned a JSON array instead of an object: {content[:200]}"
                    )
            return parsed

        assert max_tool_rounds is not None
        raise InferenceClientError(
            f"Stage '{stage_key}' exceeded max tool rounds ({max_tool_rounds})"
        )

    # ------------------------------------------------------------------
    # Context compression
    # ------------------------------------------------------------------

    async def _compress_context(
        self,
        messages: list[Message],
        source_stage_key: str,
        agent_id: str | None,
    ) -> list[Message]:
        """Summarize old tool rounds to reclaim context window space.

        Keeps the system message, the first user message (task), and the last
        4 messages verbatim. Everything in between is formatted as compact
        tool-call lines and sent to the context_summarizer stage. The result
        replaces the middle with a single injected user message.

        Never raises — returns the original list on any failure.
        """
        _KEEP_TAIL = 4   # last ~2 rounds kept verbatim so the model knows where it is
        _MIN_MIDDLE = 6  # don't bother if there's not enough to compress

        if len(messages) < 2 + _MIN_MIDDLE + _KEEP_TAIL:
            return messages

        head = messages[:2]               # system + user task
        tail = messages[-_KEEP_TAIL:]
        middle = messages[2:-_KEEP_TAIL]

        formatted = _format_tool_history(middle)

        try:
            stage_cfg = self._registry.get_stage("context_summarizer")
            driver = self._get_driver(stage_cfg.backend)
            req = InferenceRequest(
                model=stage_cfg.model,
                messages=[
                    Message(
                        role="system",
                        content="You summarize AI agent work logs concisely. Output only the summary, no preamble.",
                    ),
                    Message(
                        role="user",
                        content=(
                            "Summarize what this agent has done so far. Include: which files were read "
                            "and any key findings, which files were written and their purpose, "
                            "commands run and their outcomes. Use exact file names. Under 250 words.\n\n"
                            f"{formatted}"
                        ),
                    ),
                ],
                format="",
                temperature=stage_cfg.temperature,
                max_tokens=stage_cfg.max_tokens,
                num_ctx=stage_cfg.num_ctx,
                timeout_seconds=stage_cfg.timeout_seconds,
            )
            resp = await driver.complete(req)
            summary = (resp.content or "").strip()
        except Exception as exc:
            logger.warning(
                "compress_context: stage=%s summarizer failed (%s) — keeping full history%s",
                source_stage_key, exc, f"  agent={agent_id}" if agent_id else "",
            )
            return messages

        if not summary:
            return messages

        summary_msg = Message(
            role="user",
            content=(
                "## Prior rounds summary (context compressed)\n\n"
                f"{summary}\n\n"
                "Continue your task from where you left off."
            ),
        )

        logger.info(
            "compress_context: stage=%s compressed %d messages → 1 summary (%d chars)%s",
            source_stage_key, len(middle), len(summary),
            f"  agent={agent_id}" if agent_id else "",
        )
        return list(head) + [summary_msg] + list(tail)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_driver(self, backend: str) -> InferenceBackend:
        driver = self._drivers.get(backend)
        if driver is None:
            raise InferenceClientError(f"No driver registered for backend '{backend}'")
        return driver

    async def _log_call(
        self,
        session: AsyncSession,
        idea_id: str,
        branch_id: str | None,
        stage_result_id: str | None,
        call_type: str,
        call_index: int,
        model_name: str,
        backend: str,
        request: InferenceRequest,
        response: InferenceResponse | None,
        error: str | None,
        stage_key: str = "",
    ) -> None:
        from app.db.models import ModelCall

        prompt_json = json.dumps(self._serialize_messages_for_log(request.messages))
        response_json = json.dumps(response.raw_response if response else {"error": error})

        call = ModelCall(
            id=str(uuid.uuid4()),
            idea_id=idea_id,
            branch_id=branch_id,
            stage_result_id=stage_result_id,
            call_type=call_type,
            call_index=call_index,
            model_name=model_name,
            backend=backend,
            prompt_json=prompt_json,
            response_json=response_json,
            tokens_prompt=response.tokens_prompt if response else None,
            tokens_completion=response.tokens_completion if response else None,
            duration_ms=response.duration_ms if response else None,
            created_at=datetime.now(timezone.utc),
        )
        session.add(call)
        await session.commit()

        # Emit telemetry for the first call of each logical stage invocation.
        # Subsequent rounds (tool calls in call_with_tools) are skipped to avoid spam.
        if call_index == 0 and stage_key:
            from app import telemetry as _telemetry
            _telemetry.log_call(
                stage=stage_key,
                model=model_name,
                backend=backend,
                duration_ms=response.duration_ms if response else None,
                success=error is None,
                error=error,
                tokens_prompt=response.tokens_prompt if response else None,
                tokens_completion=response.tokens_completion if response else None,
            )

    async def _log_script_execution(
        self,
        session: AsyncSession,
        idea_id: str,
        branch_id: str | None,
        stage_result_id: str | None,
        call_index: int,
        script: str,
        result: dict,
        duration_ms: int,
    ) -> None:
        """Log a script execution as a SCRIPT_EXECUTION audit entry."""
        from app.db.models import ModelCall

        call = ModelCall(
            id=str(uuid.uuid4()),
            idea_id=idea_id,
            branch_id=branch_id,
            stage_result_id=stage_result_id,
            call_type="SCRIPT_EXECUTION",
            call_index=call_index,
            model_name="script_runner",
            backend="local",
            prompt_json=json.dumps({"type": "script", "script": script}),
            response_json=json.dumps(result),
            tokens_prompt=None,
            tokens_completion=None,
            duration_ms=duration_ms,
            created_at=datetime.now(timezone.utc),
        )
        session.add(call)
        await session.commit()

    async def _log_file_edit(
        self,
        session: AsyncSession,
        idea_id: str,
        branch_id: str | None,
        stage_result_id: str | None,
        call_index: int,
        operation: str,
        path: str,
        result: dict,
        duration_ms: int,
    ) -> None:
        """Log a file edit tool call as a FILE_EDIT audit entry."""
        from app.db.models import ModelCall

        call = ModelCall(
            id=str(uuid.uuid4()),
            idea_id=idea_id,
            branch_id=branch_id,
            stage_result_id=stage_result_id,
            call_type="FILE_EDIT",
            call_index=call_index,
            model_name="file_editor",
            backend="local",
            prompt_json=json.dumps({"type": "file_edit", "operation": operation, "path": path}),
            response_json=json.dumps(result),
            tokens_prompt=None,
            tokens_completion=None,
            duration_ms=duration_ms,
            created_at=datetime.now(timezone.utc),
        )
        session.add(call)
        await session.commit()

    async def _log_shell_command(
        self,
        session: AsyncSession,
        idea_id: str,
        branch_id: str | None,
        stage_result_id: str | None,
        call_index: int,
        command: str,
        result: dict,
        duration_ms: int,
    ) -> None:
        """Log a shell command execution as a SHELL_EXECUTION audit entry."""
        from app.db.models import ModelCall

        call = ModelCall(
            id=str(uuid.uuid4()),
            idea_id=idea_id,
            branch_id=branch_id,
            stage_result_id=stage_result_id,
            call_type="SHELL_EXECUTION",
            call_index=call_index,
            model_name="shell_runner",
            backend="local",
            prompt_json=json.dumps({"type": "shell", "command": command}),
            response_json=json.dumps(result),
            tokens_prompt=None,
            tokens_completion=None,
            duration_ms=duration_ms,
            created_at=datetime.now(timezone.utc),
        )
        session.add(call)
        await session.commit()

    async def _log_web_search(
        self,
        session: AsyncSession,
        idea_id: str,
        branch_id: str | None,
        stage_result_id: str | None,
        call_index: int,
        query: str,
        result: dict,
        duration_ms: int,
    ) -> None:
        """Log a web search as a WEB_SEARCH audit entry."""
        from app.db.models import ModelCall

        call = ModelCall(
            id=str(uuid.uuid4()),
            idea_id=idea_id,
            branch_id=branch_id,
            stage_result_id=stage_result_id,
            call_type="WEB_SEARCH",
            call_index=call_index,
            model_name="brave_search",
            backend="remote",
            prompt_json=json.dumps({"type": "web_search", "query": query}),
            response_json=json.dumps(result),
            tokens_prompt=None,
            tokens_completion=None,
            duration_ms=duration_ms,
            created_at=datetime.now(timezone.utc),
        )
        session.add(call)
        await session.commit()

    @staticmethod
    def _serialize_messages_for_log(messages: list[Message]) -> list[dict]:
        result = []
        for m in messages:
            entry: dict = {"role": m.role, "content": m.content or ""}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {"name": tc.name, "arguments": tc.arguments}
                    for tc in m.tool_calls
                ]
            result.append(entry)
        return result
