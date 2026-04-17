import json
import logging
import re
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _strip_markdown_json(text: str) -> str:
    """
    Strip markdown code fences from a model response so the content can be
    parsed as JSON.  Handles ```json ... ```, ``` ... ```, and leading/trailing
    whitespace.  Falls back to the original string if no fence is found.
    """
    stripped = text.strip()
    # Match ```json or ``` at the start, capture everything until closing ```
    m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", stripped, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Sometimes the model emits prose before the fence — grab the first fence block
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", stripped, re.DOTALL)
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

FILE_EDIT_TOOL = ToolDefinition(
    name="file_edit",
    description=(
        "Read or modify a file in the project output directory. "
        "Use this to write a new file, search-and-replace content in an existing file, "
        "or insert lines at a specific position. "
        "All paths are relative to the project root and are restricted to the project directory. "
        "Operations: "
        "'write_file' — write complete content (creates or overwrites); "
        "'search_replace' — find text and replace it (first occurrence unless replace_all=true); "
        "'insert_lines' — insert content after a given line number (-1 = append to end)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["write_file", "search_replace", "insert_lines"],
                "description": "The file operation to perform.",
            },
            "path": {
                "type": "string",
                "description": "Path relative to the project root (e.g. 'src/main.py', 'tests/test_main.py').",
            },
            "content": {
                "type": "string",
                "description": "File content for write_file; lines to insert for insert_lines.",
            },
            "search_text": {
                "type": "string",
                "description": "Text to find in the file (search_replace only).",
            },
            "replace_text": {
                "type": "string",
                "description": "Replacement text (search_replace only).",
            },
            "after_line": {
                "type": "integer",
                "description": "0-based line index to insert after (insert_lines only); -1 appends to end.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences rather than just the first (search_replace only). Default false.",
            },
        },
        "required": ["operation", "path"],
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
    ) -> dict:
        """
        Single-turn model call: send messages, get JSON back.
        Raises InferenceClientError on backend failure or non-JSON response.
        """
        stage_cfg = self._registry.get_stage(stage_key)
        driver = self._get_driver(stage_cfg.backend)

        temperature = stage_cfg.temperature
        max_tokens = stage_cfg.max_tokens
        if overrides:
            temperature = overrides.get("temperature", temperature)
            max_tokens = overrides.get("max_tokens", max_tokens)

        request = InferenceRequest(
            model=stage_cfg.model,
            messages=messages,
            format=stage_cfg.format,
            temperature=temperature,
            max_tokens=max_tokens,
            num_ctx=stage_cfg.num_ctx,
        )

        response: InferenceResponse | None = None
        error_str: str | None = None

        logger.info("call  stage=%-20s model=%s", stage_key, stage_cfg.model)
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
                model_name=stage_cfg.model,
                backend=stage_cfg.backend,
                request=request,
                response=response,
                error=error_str,
            )

        try:
            return json.loads(_strip_markdown_json(response.content))
        except json.JSONDecodeError as e:
            raise InferenceClientError(
                f"Stage '{stage_key}' returned non-JSON content: {response.content[:200]}"
            ) from e

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
    ) -> str:
        """
        Free-form text call — returns plain text instead of parsed JSON.
        Used for Phase 2 conversational responses (markdown, not structured output).
        """
        stage_cfg = self._registry.get_stage(stage_key)
        driver = self._get_driver(stage_cfg.backend)

        request = InferenceRequest(
            model=stage_cfg.model,
            messages=messages,
            format="",  # no JSON enforcement — driver skips format field
            temperature=stage_cfg.temperature,
            max_tokens=stage_cfg.max_tokens,
            num_ctx=stage_cfg.num_ctx,
        )

        response: InferenceResponse | None = None
        error_str: str | None = None

        logger.info("call_text  stage=%-20s model=%s", stage_key, stage_cfg.model)
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
        max_tool_rounds: int = 4,
        allowed_file_dir: str | None = None,
        on_tool_result=None,  # Optional[Callable[[str, dict], Awaitable[None]]]
        return_json: bool = True,
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

        # web_search is always available — uses DuckDuckGo by default, Tavily if key is set
        # file_edit and run_shell are available only when an allowed_file_dir is provided (Phase 3)
        available_tools: list[ToolDefinition] = [RUN_PYTHON_TOOL, WEB_SEARCH_TOOL]
        if allowed_file_dir:
            available_tools.append(FILE_EDIT_TOOL)
            available_tools.append(SHELL_TOOL)

        logger.info("tools stage=%-20s model=%s  tools=%s  round_limit=%d",
                    stage_key, stage_cfg.model, [t.name for t in available_tools], max_tool_rounds)
        for round_num in range(max_tool_rounds + 1):
            logger.info("tools stage=%-20s round=%d", stage_key, round_num)
            request = InferenceRequest(
                model=stage_cfg.model,
                messages=working_messages,
                format=stage_cfg.format,
                temperature=stage_cfg.temperature,
                max_tokens=stage_cfg.max_tokens,
                num_ctx=stage_cfg.num_ctx,
                tools=available_tools,
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
                        await self._log_web_search(
                            session=session, idea_id=idea_id, branch_id=branch_id,
                            stage_result_id=stage_result_id, call_index=current_call_index,
                            query=query, result=result_dict,
                            duration_ms=search_result.duration_ms,
                        )
                        current_call_index += 1
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif tc.name == "file_edit":
                        if not allowed_file_dir:
                            result_dict = {"success": False, "error": "file_edit tool not available in this context"}
                        else:
                            from app.tools.file_editor import edit_file
                            import time as _time
                            _t0 = _time.monotonic()
                            edit_result = await edit_file(
                                operation=tc.arguments.get("operation", ""),
                                path=tc.arguments.get("path", ""),
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
                                "path": edit_result.path,
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
                                path=tc.arguments.get("path", ""),
                                result=result_dict,
                                duration_ms=_duration_ms,
                            )
                            current_call_index += 1
                            if on_tool_result:
                                content_bytes = len((tc.arguments.get("content") or "").encode("utf-8"))
                                await on_tool_result("file_edit", {
                                    "path": tc.arguments.get("path", ""),
                                    "operation": edit_result.operation,
                                    "success": edit_result.success,
                                    "size_bytes": content_bytes,
                                    "detail": edit_result.detail,
                                })
                        working_messages.append(Message(role="tool", content=json.dumps(result_dict)))

                    elif tc.name == "run_shell":
                        if not allowed_file_dir:
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

                    else:
                        logger.warning("tools stage=%-20s unknown tool call: %s", stage_key, tc.name)
                        working_messages.append(Message(role="tool", content=json.dumps({"error": f"Unknown tool: {tc.name}"})))

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
                return await self.call(
                    stage_key=stage_key,
                    messages=working_messages,
                    session=session,
                    idea_id=idea_id,
                    branch_id=branch_id,
                    stage_result_id=stage_result_id,
                    call_index=current_call_index,
                )

            try:
                return json.loads(_strip_markdown_json(content))
            except json.JSONDecodeError as e:
                logger.error(
                    "tools stage=%-20s non-JSON response after tool rounds: %s",
                    stage_key, content[:300],
                )
                raise InferenceClientError(
                    f"Stage '{stage_key}' returned non-JSON after tool rounds: "
                    f"{content[:200]}"
                ) from e

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
