from app.inference.base import InferenceBackend, InferenceRequest, InferenceResponse


class OpenVinoDriver(InferenceBackend):
    """
    Post-v1 stub for Intel NPU inference via OpenVINO.
    Targets lightweight specialist models (1–3B) for parallel execution
    alongside GPU-bound orchestrator calls.
    """

    def __init__(self, model_dir: str, device: str = "NPU", timeout_seconds: int = 60) -> None:
        self._model_dir = model_dir
        self._device = device
        self._timeout = timeout_seconds

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        raise NotImplementedError(
            "OpenVINO driver is not yet implemented. "
            "Switch backend to 'ollama' or 'llamacpp' in models.yaml."
        )

    async def health_check(self) -> bool:
        return False

    async def list_available_models(self) -> list[str]:
        return []
