import asyncio
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
)

logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_JSON_BARE_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)


def _extract_json(text: str) -> str:
    m = _JSON_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _JSON_BARE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


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
        self._pipelines: dict[str, Any] = {}
        self._load_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_model_path(self, model_name: str) -> Path:
        safe = model_name.replace(":", "-")
        return self._model_dir / safe

    def _load_pipeline_sync(self, model_name: str) -> Any:
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
                f"--weight-format int4 {model_path}"
            )

        logger.info("Loading OpenVINO model '%s' on device '%s'...", model_name, self._device)
        t0 = time.monotonic()
        pipeline = ov_genai.LLMPipeline(str(model_path), self._device)
        logger.info("OpenVINO model '%s' loaded in %.1fs", model_name, time.monotonic() - t0)
        return pipeline

    async def _get_pipeline(self, model_name: str) -> Any:
        if model_name in self._pipelines:
            return self._pipelines[model_name]
        async with self._load_lock:
            if model_name in self._pipelines:
                return self._pipelines[model_name]
            loop = asyncio.get_event_loop()
            pipeline = await loop.run_in_executor(None, self._load_pipeline_sync, model_name)
            self._pipelines[model_name] = pipeline
            return pipeline

    @staticmethod
    def _build_prompt(pipeline: Any, messages: list[Message]) -> str:
        """Apply the model's chat template; fall back to generic <|role|> format."""
        try:
            tok = pipeline.get_tokenizer()
            ov_msgs = [{"role": m.role, "content": m.content or ""} for m in messages]
            result = tok.apply_chat_template(ov_msgs, add_generation_prompt=True)
            return str(result)
        except Exception:
            parts: list[str] = []
            for m in messages:
                content = m.content or ""
                if m.role == "system":
                    parts.append(f"<|system|>\n{content}\n<|end|>")
                elif m.role == "user":
                    parts.append(f"<|user|>\n{content}\n<|end|>")
                elif m.role == "assistant":
                    parts.append(f"<|assistant|>\n{content}\n<|end|>")
            parts.append("<|assistant|>")
            return "\n".join(parts)

    @staticmethod
    def _decode_result(result: Any) -> str:
        # openvino_genai ≥2024.4 returns DecodedResults with a .texts list
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

        pipeline = await self._get_pipeline(request.model)
        prompt = self._build_prompt(pipeline, request.messages)

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
        content = self._decode_result(result).strip()

        if request.format == "json":
            content = _extract_json(content)

        return InferenceResponse(
            content=content,
            model=request.model,
            tokens_prompt=None,
            tokens_completion=None,
            duration_ms=duration_ms,
            raw_response={"generated_text": content},
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
