import asyncio
import json as _json
import time

import httpx

from app.inference.base import (
    InferenceBackend,
    InferenceBackendError,
    InferenceRequest,
    InferenceResponse,
    Message,
    ToolCall,
    ToolDefinition,
)


class LlamaCppDriver(InferenceBackend):
    """llama.cpp server driver — OpenAI-compatible /v1/chat/completions endpoint.

    Differences from Ollama:
    - Streaming uses SSE (data: {...} lines, terminated by data: [DONE])
    - Tool calls carry sequential IDs; tool result messages need matching tool_call_id
    - No think= parameter — thinking models emit <think> tags in content naturally
    - num_ctx is a server-startup flag (--ctx-size), not a per-request parameter
    """

    def __init__(self, base_url: str, timeout_seconds: int = 120) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    # ------------------------------------------------------------------ build

    @staticmethod
    def _serialize_messages(messages: list[Message]) -> list[dict]:
        """Serialize to OpenAI wire format, tracking tool_call IDs across turns."""
        result: list[dict] = []
        pending_ids: list[str] = []  # IDs of tool calls awaiting a tool result message
        for m in messages:
            if m.role == "assistant" and m.tool_calls:
                tool_calls_out = []
                for i, tc in enumerate(m.tool_calls):
                    call_id = f"call_{len(result)}_{i}"
                    tool_calls_out.append({
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": _json.dumps(tc.arguments),
                        },
                    })
                    pending_ids.append(call_id)
                result.append({
                    "role": "assistant",
                    "content": m.content or "",
                    "tool_calls": tool_calls_out,
                })
            elif m.role == "tool":
                call_id = pending_ids.pop(0) if pending_ids else "call_0"
                result.append({
                    "role": "tool",
                    "content": m.content or "",
                    "tool_call_id": call_id,
                })
            else:
                result.append({"role": m.role, "content": m.content or ""})
        return result

    def _build_payload(self, request: InferenceRequest, stream: bool) -> dict:
        payload: dict = {
            "model": request.model,
            "messages": self._serialize_messages(request.messages),
            "temperature": request.temperature,
            "stream": stream,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
            payload["tool_choice"] = "auto"
        elif request.format == "json":
            payload["response_format"] = {"type": "json_object"}
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        return payload

    @staticmethod
    def _parse_tool_calls(raw: list) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for tc in raw:
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = _json.loads(args)
                except Exception:
                    args = {"_raw": args}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args))
        return calls

    # ---------------------------------------------------------------- complete

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        if request.on_token is not None:
            return await self._complete_streaming(request)
        return await self._complete_blocking(request)

    async def _complete_blocking(self, request: InferenceRequest) -> InferenceResponse:
        payload = self._build_payload(request, stream=False)
        effective_timeout = request.timeout_seconds if request.timeout_seconds is not None else self._timeout
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                resp = await client.post(f"{self._base_url}/v1/chat/completions", json=payload)
                if not resp.is_success:
                    raise InferenceBackendError(
                        f"llama.cpp request failed: {resp.status_code} — {resp.text[:500]}"
                    )
        except httpx.TimeoutException as e:
            raise InferenceBackendError(
                f"llama.cpp request timed out after {effective_timeout}s (model={request.model})"
            ) from e
        except httpx.HTTPError as e:
            raise InferenceBackendError(
                f"llama.cpp request failed: {str(e) or type(e).__name__} (model={request.model})"
            ) from e

        duration_ms = int((time.monotonic() - start) * 1000)
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        usage = data.get("usage", {})

        return InferenceResponse(
            content=message.get("content") or "",
            model=data.get("model", request.model),
            tokens_prompt=usage.get("prompt_tokens"),
            tokens_completion=usage.get("completion_tokens"),
            duration_ms=duration_ms,
            raw_response=data,
            tool_calls=self._parse_tool_calls(message.get("tool_calls") or []),
        )

    async def _complete_streaming(self, request: InferenceRequest) -> InferenceResponse:
        """Stream content tokens via SSE to on_token callback; return full InferenceResponse."""
        payload = self._build_payload(request, stream=True)
        effective_timeout = request.timeout_seconds if request.timeout_seconds is not None else self._timeout
        start = time.monotonic()
        content_parts: list[str] = []
        tool_call_accum: dict[int, dict] = {}  # index → {id, function: {name, arguments}}
        finish_reason: str | None = None
        usage: dict = {}

        # Repetition-loop detector — same thresholds as Ollama driver
        _REPLOOP_CHECK_AT = 8_000
        _REPLOOP_ABORT_AT = 20_000
        _reploop_checked = False

        # 120s per-chunk read timeout; asyncio.wait_for enforces the hard wall-clock cap
        _chunk_timeout = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=10.0)

        async def _run() -> None:
            nonlocal finish_reason, usage, _reploop_checked
            try:
                async with httpx.AsyncClient(timeout=_chunk_timeout) as client:
                    async with client.stream("POST", f"{self._base_url}/v1/chat/completions", json=payload) as resp:
                        if not resp.is_success:
                            body = await resp.aread()
                            raise InferenceBackendError(
                                f"llama.cpp request failed: {resp.status_code} — {body[:500].decode()}"
                            )
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            raw = line[6:].strip()
                            if raw == "[DONE]":
                                break
                            try:
                                data = _json.loads(raw)
                            except _json.JSONDecodeError:
                                continue

                            choice = (data.get("choices") or [{}])[0]
                            delta = choice.get("delta", {})
                            finish_reason = choice.get("finish_reason") or finish_reason
                            if "usage" in data:
                                usage.update(data["usage"])

                            # Accumulate tool call fragments (name + arguments arrive piece by piece)
                            for tc in delta.get("tool_calls") or []:
                                idx = tc.get("index", 0)
                                if idx not in tool_call_accum:
                                    tool_call_accum[idx] = {
                                        "id": tc.get("id", ""),
                                        "function": {"name": "", "arguments": ""},
                                    }
                                fn = tc.get("function", {})
                                tool_call_accum[idx]["function"]["name"] += fn.get("name") or ""
                                tool_call_accum[idx]["function"]["arguments"] += fn.get("arguments") or ""

                            content_chunk = delta.get("content") or ""
                            if content_chunk:
                                content_parts.append(content_chunk)
                                if request.on_token:
                                    request.on_token(content_chunk)

                                _content_len = sum(len(p) for p in content_parts)
                                if _content_len >= _REPLOOP_ABORT_AT:
                                    raise InferenceBackendError(
                                        f"llama.cpp model '{request.model}' repetition loop: "
                                        f"content reached {_content_len} chars without finishing"
                                    )
                                if not _reploop_checked and _content_len >= _REPLOOP_CHECK_AT:
                                    _reploop_checked = True
                                    _recent = "".join(content_parts)[-2000:]
                                    _words = _recent.split()
                                    if len(_words) >= 50:
                                        _unique_ratio = len(set(_words)) / len(_words)
                                        if _unique_ratio < 0.08:
                                            raise InferenceBackendError(
                                                f"llama.cpp model '{request.model}' repetition loop: "
                                                f"{_content_len} chars, {_unique_ratio:.0%} unique words in last 2k chars"
                                            )
            except httpx.TimeoutException as e:
                raise InferenceBackendError(
                    f"llama.cpp request timed out (model={request.model})"
                ) from e
            except httpx.HTTPError as e:
                raise InferenceBackendError(
                    f"llama.cpp request failed: {str(e) or type(e).__name__} (model={request.model})"
                ) from e

        try:
            await asyncio.wait_for(_run(), timeout=float(effective_timeout))
        except asyncio.TimeoutError:
            raise InferenceBackendError(
                f"llama.cpp streaming timed out after {effective_timeout}s (model={request.model})"
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        return InferenceResponse(
            content="".join(content_parts),
            model=request.model,
            tokens_prompt=usage.get("prompt_tokens"),
            tokens_completion=usage.get("completion_tokens"),
            duration_ms=duration_ms,
            raw_response={"choices": [{"finish_reason": finish_reason}], "usage": usage},
            tool_calls=self._parse_tool_calls(list(tool_call_accum.values())),
        )

    # ------------------------------------------------------------------- util

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._base_url}/health")
                return resp.status_code == 200
        except Exception:
            return False

    async def list_available_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self._base_url}/v1/models")
                resp.raise_for_status()
                return [m["id"] for m in resp.json().get("data", [])]
        except Exception:
            return []
