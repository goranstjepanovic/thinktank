import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from app.inference.base import (
    InferenceBackend,
    InferenceBackendError,
    InferenceRequest,
    InferenceResponse,
    Message,
    ToolCall,
    ToolDefinition,
)

logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_JSON_BARE_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)
# Qwen3 / Mistral-style tool call tags
_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
# Strip think/reasoning blocks before parsing
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _extract_json(text: str) -> str:
    m = _JSON_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _JSON_BARE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _parse_tool_calls(text: str) -> list[ToolCall]:
    """
    Extract tool calls from model output.

    Handles two formats:
      1. <tool_call>{"name": "...", "arguments": {...}}</tool_call>  (Qwen3, Mistral)
      2. Top-level JSON {"tool_calls": [{"function": {"name": ..., "arguments": ...}}]}
    """
    text = _THINK_RE.sub("", text).strip()
    calls: list[ToolCall] = []

    for m in _TOOL_CALL_TAG_RE.finditer(text):
        try:
            obj = json.loads(m.group(1))
            name = obj.get("name", "")
            args = obj.get("arguments") or obj.get("parameters") or {}
            if isinstance(args, str):
                args = json.loads(args)
            calls.append(ToolCall(name=name, arguments=args))
        except Exception:
            continue

    if calls:
        return calls

    try:
        obj = json.loads(_extract_json(text))
        if isinstance(obj, dict) and "tool_calls" in obj:
            for tc in obj["tool_calls"]:
                fn = tc.get("function", tc)
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    args = json.loads(args)
                calls.append(ToolCall(name=name, arguments=args))
    except Exception:
        pass

    return calls


def _tools_payload(tools: list[ToolDefinition]) -> list[dict]:
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


class OpenVinoDriver(InferenceBackend):
    """
    Intel OpenVINO GenAI driver for local LLM inference.

    Targets Intel NPU, integrated Arc GPU, or CPU — not NVIDIA.
    Models must be pre-converted to OpenVINO IR format:

        pip install optimum[openvino]
        optimum-cli export openvino --model <hf-model-id> \\
            --weight-format int4 <model_dir>/<model-name>

    Set backend: openvino in a stage to route it here.
    The stage's `model` field maps to a subdirectory under model_dir.
    Colons in model names are replaced with dashes (e.g. qwen2.5:7b → qwen2.5-7b).

    Tool calling is supported for models whose chat template handles a `tools`
    argument (Qwen3, Mistral, Llama-3.1+). The driver parses <tool_call> tags
    from the generated output and returns ToolCall objects.

    Device options (set in models.yaml backends.openvino.device):
        NPU              — Intel AI Boost NPU (fastest for INT4, power-efficient)
        GPU              — Intel integrated Arc GPU
        CPU              — CPU fallback (always available)
        AUTO:NPU,GPU,CPU — tries each in order; recommended for flexibility
    """

    def __init__(self, model_dir: str, device: str = "AUTO:NPU,GPU,CPU", timeout_seconds: int = 300) -> None:
        self._model_dir = Path(model_dir)
        self._device = device
        self._timeout = timeout_seconds
        self._pipelines: dict[tuple[str, str], Any] = {}  # keyed by (model_name, device)
        self._load_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_model_path(self, model_name: str) -> Path:
        safe = model_name.replace(":", "-")
        return self._model_dir / safe

    def _load_pipeline_sync(self, model_name: str, device: str) -> Any:
        try:
            import openvino_genai as ov_genai
        except ImportError as exc:
            raise InferenceBackendError(
                "openvino-genai is not installed. Run: pip install openvino-genai"
            ) from exc

        model_path = self._resolve_model_path(model_name)
        if not model_path.exists():
            raise InferenceBackendError(
                f"OpenVINO model not found: {model_path}\n"
                f"Convert from HuggingFace:\n"
                f"  optimum-cli export openvino --model <hf-model-id> "
                f"--weight-format int4_mixed {model_path}"
            )

        logger.info("Loading OpenVINO model '%s' on device '%s'...", model_name, device)
        t0 = time.monotonic()
        pipeline = ov_genai.LLMPipeline(str(model_path), device)
        logger.info("OpenVINO model '%s' loaded in %.1fs", model_name, time.monotonic() - t0)
        return pipeline

    async def _get_pipeline(self, model_name: str, device: str) -> Any:
        key = (model_name, device)
        if key in self._pipelines:
            return self._pipelines[key]
        async with self._load_lock:
            if key in self._pipelines:
                return self._pipelines[key]
            loop = asyncio.get_event_loop()
            pipeline = await loop.run_in_executor(None, self._load_pipeline_sync, model_name, device)
            self._pipelines[key] = pipeline
            return pipeline

    @staticmethod
    def _serialize_message(m: Message) -> dict:
        msg: dict = {"role": m.role, "content": m.content or ""}
        if m.tool_calls:
            msg["tool_calls"] = [
                {"type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in m.tool_calls
            ]
        return msg

    @staticmethod
    def _build_prompt(
        pipeline: Any,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
    ) -> str:
        """Apply the model's chat template, optionally injecting tool schemas."""
        try:
            tok = pipeline.get_tokenizer()
            ov_msgs = [OpenVinoDriver._serialize_message(m) for m in messages]
            kwargs: dict[str, Any] = {"add_generation_prompt": True}
            if tools:
                try:
                    kwargs["tools"] = _tools_payload(tools)
                    result = tok.apply_chat_template(ov_msgs, **kwargs)
                    return str(result)
                except Exception:
                    # Chat template doesn't accept tools kwarg — fall back without
                    kwargs.pop("tools", None)
            result = tok.apply_chat_template(ov_msgs, **kwargs)
            return str(result)
        except Exception:
            # Generic fallback for models with no HF chat template
            parts: list[str] = []
            for m in messages:
                content = m.content or ""
                if m.role == "system":
                    parts.append(f"<|system|>\n{content}\n<|end|>")
                elif m.role == "user":
                    parts.append(f"<|user|>\n{content}\n<|end|>")
                elif m.role == "assistant":
                    parts.append(f"<|assistant|>\n{content}\n<|end|>")
                elif m.role == "tool":
                    parts.append(f"<|tool|>\n{content}\n<|end|>")
            parts.append("<|assistant|>")
            return "\n".join(parts)

    @staticmethod
    def _decode_result(result: Any) -> str:
        if hasattr(result, "texts"):
            return result.texts[0] if result.texts else ""
        return str(result)

    # ------------------------------------------------------------------
    # InferenceBackend contract
    # ------------------------------------------------------------------

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        try:
            import openvino_genai as ov_genai
        except ImportError as exc:
            raise InferenceBackendError(
                "openvino-genai is not installed. Run: pip install openvino-genai"
            ) from exc

        effective_device = (request.extra or {}).get("device") or self._device
        pipeline = await self._get_pipeline(request.model, effective_device)
        prompt = self._build_prompt(pipeline, request.messages, tools=request.tools or None)

        config = ov_genai.GenerationConfig()
        config.temperature = float(request.temperature)
        config.do_sample = request.temperature > 0
        if request.max_tokens is not None:
            config.max_new_tokens = request.max_tokens

        effective_timeout = (
            request.timeout_seconds if request.timeout_seconds is not None else self._timeout
        )

        t0 = time.monotonic()
        loop = asyncio.get_event_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: pipeline.generate(prompt, config)),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise InferenceBackendError(
                f"OpenVINO inference timed out after {effective_timeout}s (model={request.model})"
            ) from exc
        except Exception as exc:
            raise InferenceBackendError(
                f"OpenVINO inference error (model={request.model}): {exc}"
            ) from exc

        duration_ms = int((time.monotonic() - t0) * 1000)
        content = _THINK_RE.sub("", self._decode_result(result)).strip()

        tool_calls: list[ToolCall] = []
        if request.tools:
            tool_calls = _parse_tool_calls(content)
            if tool_calls:
                content = ""

        if not tool_calls and request.format == "json":
            content = _extract_json(content)

        return InferenceResponse(
            content=content,
            model=request.model,
            tokens_prompt=None,
            tokens_completion=None,
            duration_ms=duration_ms,
            raw_response={"generated_text": content},
            tool_calls=tool_calls,
        )

    async def health_check(self) -> bool:
        try:
            import openvino_genai  # noqa: F401
        except ImportError:
            return False
        return self._model_dir.exists()

    async def list_available_models(self) -> list[str]:
        if not self._model_dir.exists():
            return []
        return sorted(
            d.name
            for d in self._model_dir.iterdir()
            if d.is_dir() and (d / "openvino_model.xml").exists()
        )
