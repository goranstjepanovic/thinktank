import asyncio
import json
import logging
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
    Strip <think> blocks and markdown code fences from a model response so
    the content can be parsed as JSON.  Handles ```json ... ```, ``` ... ```,
    and leading/trailing whitespace.  Falls back to the original string if no
    fence is found.
    """
    stripped = text.strip()
    # Strip <think>...</think> blocks produced by reasoning models (e.g. qwen3.5)
    stripped = re.sub(r"<think>.*?</think>", "", stripped, flags=re.DOTALL).strip()
    # If the response already looks like JSON, do not scan for code fences.
    # JSON string values may legitimately mention ``` markers.
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
        "Read the contents of a file in the project directory. "
        "Use this to inspect a specific file before deciding what to change. "
        "Large files are automatically truncated."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (e.g. 'backend/app/main.py').",
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


class InferenceClientError(Exception):
    pass


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
        on_tool_result=None,  # Optional[Callable[[str, dict], Awaitable[None]]]
        return_json: bool = True,
        extra_tools: "list[ToolDefinition] | None" = None,
        custom_tool_handlers: "dict | None" = None,  # {name: async callable(args) -> dict}
        model_override: str | None = None,
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
                available_tools += [FILE_EDIT_TOOL, DELETE_PATH_TOOL, SHELL_TOOL, RUN_SHELL_BG_TOOL, GET_SHELL_OUTPUT_TOOL, STOP_SHELL_PROCESS_TOOL]
        if extra_tools:
            available_tools += extra_tools
            # If inspect_files is provided, remove read_file so the model is forced
            # to batch file reads via inspect_files (returns content in one round)
            # instead of calling read_file one file per round.
            if any(t.name == "inspect_files" for t in extra_tools):
                available_tools = [t for t in available_tools if t.name != "read_file"]

        effective_model = model_override or stage_cfg.model
        logger.info("tools stage=%-20s model=%s  tools=%s  round_limit=%s",
                    stage_key, effective_model, [t.name for t in available_tools], max_tool_rounds if max_tool_rounds is not None else "unlimited")
        round_num = 0
        while max_tool_rounds is None or round_num <= max_tool_rounds:
            logger.info("tools stage=%-20s round=%d", stage_key, round_num)
            request = InferenceRequest(
                model=effective_model,
                messages=working_messages,
                format=stage_cfg.format,
                temperature=stage_cfg.temperature,
                max_tokens=stage_cfg.max_tokens,
                num_ctx=stage_cfg.num_ctx,
                tools=available_tools,
                timeout_seconds=stage_cfg.timeout_seconds,
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
                    call_index=current_call_index, model_name=stage_cfg.model,
                    backend=stage_cfg.backend, request=request,
                    response=None, error=error_str,
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
                call_index=current_call_index, model_name=stage_cfg.model,
                backend=stage_cfg.backend, request=request,
                response=response, error=None,
            )
            current_call_index += 1

            # Model called a tool — execute each, append results, loop
            if response.tool_calls:
                # Append the assistant's tool-call turn to the conversation
                working_messages.append(
                    Message(role="assistant", content="", tool_calls=response.tool_calls)
                )

                for tc in response.tool_calls:
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
                            logger.info("tools stage=%-20s list_files path=%r → %d entries",
                                        stage_key, _rel or ".", len(result_dict.get("entries", [])))
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
                            _MAX_READ = 12_000
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
                                        text = raw_bytes[:_MAX_READ].decode("utf-8", errors="replace")
                                        truncated = len(raw_bytes) > _MAX_READ
                                        result_dict = {
                                            "path": _rel,
                                            "content": text + ("\n... (truncated)" if truncated else ""),
                                            "size_bytes": len(raw_bytes),
                                            "truncated": truncated,
                                        }
                                except Exception as _e:
                                    result_dict = {"error": f"failed to read: {_e}"}
                            logger.info("tools stage=%-20s read_file path=%r", stage_key, _rel)
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
                            logger.info("tools stage=%-20s grep_files pattern=%r → %d matches",
                                        stage_key, _pattern, len(result_dict.get("matches", [])))
                            if on_tool_result:
                                await on_tool_result("grep_files", {
                                    "pattern": _pattern,
                                    "match_count": len(result_dict.get("matches", [])),
                                })
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
            content = response.content.strip() if response.content else ""

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
