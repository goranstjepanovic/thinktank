import time

import httpx

from app.inference.base import InferenceBackend, InferenceBackendError, InferenceRequest, InferenceResponse


class LlamaCppDriver(InferenceBackend):
    """llama.cpp server driver using its OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(self, base_url: str, timeout_seconds: int = 120) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        payload = {
            "model": request.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": request.temperature,
            "response_format": {"type": "json_object"},
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(f"{self._base_url}/v1/chat/completions", json=payload)
                response.raise_for_status()
        except httpx.HTTPError as e:
            raise InferenceBackendError(f"llama.cpp request failed: {e}") from e

        duration_ms = int((time.monotonic() - start) * 1000)
        data = response.json()

        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})

        return InferenceResponse(
            content=content,
            model=data.get("model", request.model),
            tokens_prompt=usage.get("prompt_tokens"),
            tokens_completion=usage.get("completion_tokens"),
            duration_ms=duration_ms,
            raw_response=data,
        )

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self._base_url}/health")
                return response.status_code == 200
        except Exception:
            return False

    async def list_available_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self._base_url}/v1/models")
                response.raise_for_status()
                data = response.json()
                return [m["id"] for m in data.get("data", [])]
        except Exception:
            return []
