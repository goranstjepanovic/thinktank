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

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        payload = {
            "model": request.model,
            "messages": self._serialize_messages(request.messages),
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "num_ctx": request.num_ctx,
            },
        }
        # Only set format when explicitly "json" and there are no tools.
        # Tool-use responses don't go through Ollama's JSON formatter.
        # Free-form (Phase 2 chat) responses omit format entirely.
        if request.tools:
            payload["tools"] = self._build_tools_payload(request.tools)
        elif request.format == "json":
            payload["format"] = "json"

        if request.max_tokens is not None:
            payload["options"]["num_predict"] = request.max_tokens
        if request.extra:
            payload.update(request.extra)

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(f"{self._base_url}/api/chat", json=payload)
                if not resp.is_success:
                    body = resp.text[:500]
                    raise InferenceBackendError(
                        f"Ollama request failed: {resp.status_code} — {body}"
                    )
                response = resp
        except httpx.TimeoutException as e:
            raise InferenceBackendError(f"Ollama request timed out after {self._timeout}s (model={request.model})") from e
        except httpx.HTTPError as e:
            detail = str(e) or type(e).__name__
            raise InferenceBackendError(f"Ollama request failed: {detail} (model={request.model})") from e

        duration_ms = int((time.monotonic() - start) * 1000)
        data = response.json()

        message = data.get("message", {})
        content = message.get("content") or ""
        usage = data.get("usage", {})

        # Parse tool_calls if the model chose to invoke a tool
        tool_calls: list[ToolCall] = []
        raw_tool_calls = message.get("tool_calls") or []
        for tc in raw_tool_calls:
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            # Ollama may return arguments as a string (JSON-encoded) or dict
            if isinstance(args, str):
                import json
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}
            tool_calls.append(ToolCall(name=fn.get("name", ""), arguments=args))

        return InferenceResponse(
            content=content,
            model=data.get("model", request.model),
            tokens_prompt=usage.get("prompt_tokens"),
            tokens_completion=usage.get("completion_tokens"),
            duration_ms=duration_ms,
            raw_response=data,
            tool_calls=tool_calls,
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

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("POST", f"{self._base_url}/api/chat", json=payload) as resp:
                    if not resp.is_success:
                        body = await resp.aread()
                        raise InferenceBackendError(
                            f"Ollama streaming failed: {resp.status_code} — {body[:500].decode()}"
                        )
                    async for line in resp.aiter_lines():
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
            raise InferenceBackendError(f"Ollama streaming timed out after {self._timeout}s (model={request.model})") from e
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
