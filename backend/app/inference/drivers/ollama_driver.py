import asyncio
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


class OllamaDriver(InferenceBackend):
    def __init__(self, base_url: str, timeout_seconds: int = 120) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def _build_payload(self, request: InferenceRequest, stream: bool) -> dict:
        payload: dict = {
            "model": request.model,
            "messages": self._serialize_messages(request.messages),
            "stream": stream,
            "options": {
                "temperature": request.temperature,
                "num_ctx": request.num_ctx,
            },
        }
        if request.tools:
            payload["tools"] = self._build_tools_payload(request.tools)
        elif request.format == "json":
            payload["format"] = "json"
        if request.max_tokens is not None:
            payload["options"]["num_predict"] = request.max_tokens
        if request.extra:
            payload.update(request.extra)
        # Always send think explicitly so thinking models (e.g. qwen3.x) don't default to on
        payload["think"] = request.think
        return payload

    @staticmethod
    def _parse_tool_calls(raw: list) -> list[ToolCall]:
        import json as _json
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
                resp = await client.post(f"{self._base_url}/api/chat", json=payload)
                if not resp.is_success:
                    body = resp.text[:500]
                    raise InferenceBackendError(
                        f"Ollama request failed: {resp.status_code} — {body}"
                    )
        except httpx.TimeoutException as e:
            raise InferenceBackendError(f"Ollama request timed out after {effective_timeout}s (model={request.model})") from e
        except httpx.HTTPError as e:
            raise InferenceBackendError(f"Ollama request failed: {str(e) or type(e).__name__} (model={request.model})") from e

        duration_ms = int((time.monotonic() - start) * 1000)
        data = resp.json()
        message = data.get("message", {})
        return InferenceResponse(
            content=message.get("content") or "",
            model=data.get("model", request.model),
            tokens_prompt=data.get("prompt_eval_count"),
            tokens_completion=data.get("eval_count"),
            duration_ms=duration_ms,
            raw_response=data,
            tool_calls=self._parse_tool_calls(message.get("tool_calls") or []),
        )

    async def _complete_streaming(self, request: InferenceRequest) -> InferenceResponse:
        """Stream content tokens to on_token callback; return full InferenceResponse from final chunk."""
        import json as _json

        payload = self._build_payload(request, stream=True)
        effective_timeout = request.timeout_seconds if request.timeout_seconds is not None else self._timeout
        start = time.monotonic()
        content_parts: list[str] = []
        final_data: dict = {}
        _think_open: bool = False  # True while inside a thinking block
        # Repetition-loop detector: models occasionally collapse into repeating the
        # same phrase thousands of times. Abort if content grows large AND is non-diverse.
        _REPLOOP_CHECK_AT = 8_000   # chars before we start checking
        _REPLOOP_ABORT_AT = 20_000  # hard abort regardless of diversity
        _reploop_checked = False

        # 120s per-chunk read timeout catches a silently stalled connection.
        # asyncio.wait_for below enforces the hard wall-clock cap on total generation time
        # (httpx's scalar timeout is per-chunk only, so it resets on every token).
        _chunk_timeout = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=10.0)

        async def _run() -> None:
            nonlocal final_data, _think_open, _reploop_checked
            try:
                async with httpx.AsyncClient(timeout=_chunk_timeout) as client:
                    async with client.stream("POST", f"{self._base_url}/api/chat", json=payload) as resp:
                        if not resp.is_success:
                            body = await resp.aread()
                            raise InferenceBackendError(
                                f"Ollama request failed: {resp.status_code} — {body[:500].decode()}"
                            )
                        async for line in resp.aiter_lines():
                            if not line:
                                continue
                            try:
                                data = _json.loads(line)
                            except _json.JSONDecodeError:
                                continue
                            msg = data.get("message", {})
                            thinking_chunk = msg.get("thinking") or ""
                            content_chunk = msg.get("content") or ""

                            if thinking_chunk:
                                # Wrap thinking tokens in <think> tags so the existing
                                # _extract_think_content mechanism in callers handles them.
                                if not _think_open and request.on_token:
                                    request.on_token("<think>")
                                    _think_open = True
                                if request.on_token:
                                    request.on_token(thinking_chunk)

                            if content_chunk:
                                if _think_open and request.on_token:
                                    request.on_token("</think>")
                                    _think_open = False
                                content_parts.append(content_chunk)
                                if request.on_token:
                                    request.on_token(content_chunk)

                                # Repetition-loop detection: check diversity once content crosses
                                # the soft threshold, then hard-abort at the hard threshold.
                                _content_len = sum(len(p) for p in content_parts)
                                if _content_len >= _REPLOOP_ABORT_AT:
                                    raise InferenceBackendError(
                                        f"Ollama model '{request.model}' repetition loop: "
                                        f"content reached {_content_len} chars without finishing "
                                        f"(model={request.model})"
                                    )
                                if not _reploop_checked and _content_len >= _REPLOOP_CHECK_AT:
                                    _reploop_checked = True
                                    _recent = "".join(content_parts)[-2000:]
                                    _words = _recent.split()
                                    if len(_words) >= 50:
                                        _unique_ratio = len(set(_words)) / len(_words)
                                        if _unique_ratio < 0.08:
                                            raise InferenceBackendError(
                                                f"Ollama model '{request.model}' repetition loop: "
                                                f"{_content_len} chars, {_unique_ratio:.0%} unique words in last 2k chars "
                                                f"(model={request.model})"
                                            )

                            if data.get("done"):
                                if _think_open and request.on_token:
                                    request.on_token("</think>")
                                final_data = data
                                break
            except httpx.TimeoutException as e:
                raise InferenceBackendError(f"Ollama request timed out (model={request.model})") from e
            except httpx.HTTPError as e:
                raise InferenceBackendError(f"Ollama request failed: {str(e) or type(e).__name__} (model={request.model})") from e

        try:
            await asyncio.wait_for(_run(), timeout=float(effective_timeout))
        except asyncio.TimeoutError:
            raise InferenceBackendError(
                f"Ollama streaming timed out after {effective_timeout}s (model={request.model})"
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        # Tool calls are delivered in the final done:true message
        final_msg = final_data.get("message", {})
        return InferenceResponse(
            content="".join(content_parts),
            model=final_data.get("model", request.model),
            tokens_prompt=final_data.get("prompt_eval_count"),
            tokens_completion=final_data.get("eval_count"),
            duration_ms=duration_ms,
            raw_response=final_data,
            tool_calls=self._parse_tool_calls(final_msg.get("tool_calls") or []),
        )

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_messages(messages: list[Message]) -> list[dict]:
        result = []
        for m in messages:
            msg: dict = {"role": m.role, "content": m.content or ""}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {"function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in m.tool_calls
                ]
            result.append(msg)
        return result

    @staticmethod
    def _build_tools_payload(tools: list[ToolDefinition]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    async def stream_complete(self, request: InferenceRequest):
        """Stream response chunks via Ollama's streaming API."""
        import json as _json

        payload = {
            "model": request.model,
            "messages": self._serialize_messages(request.messages),
            "stream": True,
            "options": {
                "temperature": request.temperature,
                "num_ctx": request.num_ctx,
            },
        }
        if request.format == "json":
            payload["format"] = "json"
        if request.max_tokens is not None:
            payload["options"]["num_predict"] = request.max_tokens

        effective_timeout = request.timeout_seconds if request.timeout_seconds is not None else self._timeout
        deadline = time.monotonic() + effective_timeout
        # Per-chunk read timeout catches a stalled connection; deadline below caps total wall-clock time.
        _chunk_timeout = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=10.0)
        try:
            async with httpx.AsyncClient(timeout=_chunk_timeout) as client:
                async with client.stream("POST", f"{self._base_url}/api/chat", json=payload) as resp:
                    if not resp.is_success:
                        body = await resp.aread()
                        raise InferenceBackendError(
                            f"Ollama streaming failed: {resp.status_code} — {body[:500].decode()}"
                        )
                    async for line in resp.aiter_lines():
                        if time.monotonic() > deadline:
                            raise InferenceBackendError(
                                f"Ollama streaming timed out after {effective_timeout}s (model={request.model})"
                            )
                        if not line:
                            continue
                        try:
                            data = _json.loads(line)
                        except _json.JSONDecodeError:
                            continue
                        chunk = data.get("message", {}).get("content", "")
                        if chunk:
                            yield chunk
                        if data.get("done"):
                            break
        except httpx.TimeoutException as e:
            raise InferenceBackendError(f"Ollama streaming timed out after {effective_timeout}s (model={request.model})") from e
        except httpx.HTTPError as e:
            detail = str(e) or type(e).__name__
            raise InferenceBackendError(f"Ollama streaming failed: {detail} (model={request.model})") from e

    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                return response.status_code == 200
        except Exception:
            return False

    async def list_available_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                response.raise_for_status()
                data = response.json()
                return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []
