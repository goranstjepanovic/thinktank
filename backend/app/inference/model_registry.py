import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class SelectableModel:
    name: str
    model: str
    description: str = ""
    timeout_seconds: int | None = None
    num_ctx: int | None = None
    think: bool = False  # request extended thinking from Ollama for this model


@dataclass
class StageConfig:
    model: str
    backend: str
    temperature: float
    max_tokens: int | None
    format: str
    num_ctx: int = 8192
    supports_tools: bool = False
    fallback_models: list["SelectableModel"] = field(default_factory=list)
    selectable_models: list[SelectableModel] = field(default_factory=list)
    timeout_seconds: int | None = None  # overrides backend default when set
    extra: dict = field(default_factory=dict)  # passed verbatim to the backend payload


@dataclass
class BackendConfig:
    base_url: str | None
    timeout_seconds: int
    extra: dict


@dataclass
class QuantizationConfig:
    large_model_min_b: int | None = 14
    large_model_quant: str = "q4_K_M"


@dataclass
class ResourceConfig:
    max_concurrent_branches: int
    initial_branches_per_idea: int
    max_parallel_sub_agents: int = 1
    max_verify_fix_cycles: int = 3


def apply_quant_suffix(model: str, quant_level: str) -> str:
    """Append a quantization suffix to an Ollama model tag.

    Only call this when you know the quantized variant has been pulled.
    E.g. apply_quant_suffix("qwen3.6:27b", "q4_K_M") → "qwen3.6:27b-q4_K_M"
    No-ops if the tag already contains a quant suffix or has no ':' separator.
    """
    if not quant_level or ":" not in model:
        return model
    name, tag = model.split(":", 1)
    if re.search(r"-q\d", tag, re.IGNORECASE):
        return model
    return f"{name}:{tag}-{quant_level}"


class ModelRegistry:
    def __init__(self, yaml_path: Path) -> None:
        self._yaml_path = yaml_path
        self._defaults: dict = {}
        self._backends: dict[str, BackendConfig] = {}
        self._stages: dict[str, StageConfig] = {}
        self._quant: QuantizationConfig | None = None
        self.resources = ResourceConfig(
            max_concurrent_branches=4,
            initial_branches_per_idea=2,
        )
        self._load()

    def _load(self) -> None:
        with open(self._yaml_path) as f:
            data = yaml.safe_load(f)

        self._defaults = data.get("defaults", {})

        backends: dict[str, BackendConfig] = {}
        for name, cfg in data.get("backends", {}).items():
            backends[name] = BackendConfig(
                base_url=cfg.get("base_url"),
                timeout_seconds=cfg.get("timeout_seconds", 120),
                extra={k: v for k, v in cfg.items() if k not in ("base_url", "timeout_seconds")},
            )

        qcfg = data.get("quantization") or {}
        quant = QuantizationConfig(
            large_model_min_b=qcfg.get("large_model_min_b", 14),
            large_model_quant=qcfg.get("large_model_quant", "q4_K_M"),
        ) if qcfg else None

        _KNOWN = {"model", "backend", "temperature", "max_tokens", "format",
                  "num_ctx", "supports_tools", "fallback_models", "selectable_models", "models", "timeout_seconds"}
        stages: dict[str, StageConfig] = {}
        for stage_key, cfg in data.get("stages", {}).items():
            backend = cfg["backend"]
            # Unified 'models' list supersedes legacy selectable_models + fallback_models
            raw_models_list = cfg.get("models") or []
            if raw_models_list:
                selectable = [
                    SelectableModel(
                        name=s.get("name", s["model"]), model=s["model"],
                        description=s.get("description", ""),
                        timeout_seconds=s.get("timeout_seconds"), num_ctx=s.get("num_ctx"),
                        think=bool(s.get("think", False)),
                    )
                    for s in raw_models_list
                ]
                fallbacks = []
            else:
                selectable = [
                    SelectableModel(
                        name=s["name"], model=s["model"], description=s.get("description", ""),
                        timeout_seconds=s.get("timeout_seconds"), num_ctx=s.get("num_ctx"),
                        think=bool(s.get("think", False)),
                    )
                    for s in (cfg.get("selectable_models") or [])
                ]
                raw_fallbacks = cfg.get("fallback_models") or []
                fallbacks = [
                    SelectableModel(
                        name=f if isinstance(f, str) else f["model"],
                        model=f if isinstance(f, str) else f["model"],
                        timeout_seconds=None if isinstance(f, str) else f.get("timeout_seconds"),
                        num_ctx=None if isinstance(f, str) else f.get("num_ctx"),
                    )
                    for f in raw_fallbacks
                ]
            # Derive primary model from first entry when no explicit 'model' key
            model = cfg.get("model") or (selectable[0].model if selectable else None)
            if not model:
                raise ValueError(f"Stage '{stage_key}' must define either 'model' or 'selectable_models'")
            stages[stage_key] = StageConfig(
                model=model,
                backend=backend,
                temperature=cfg.get("temperature", self._defaults.get("temperature", 0.2)),
                max_tokens=cfg.get("max_tokens", self._defaults.get("max_tokens")),
                format=cfg.get("format", self._defaults.get("format", "json")),
                num_ctx=cfg.get("num_ctx", self._defaults.get("num_ctx", 8192)),
                supports_tools=cfg.get("supports_tools", self._defaults.get("supports_tools", False)),
                fallback_models=fallbacks,
                selectable_models=selectable,
                timeout_seconds=cfg.get("timeout_seconds"),
                extra={k: v for k, v in cfg.items() if k not in _KNOWN},
            )

        res = data.get("resources", {})
        resources = ResourceConfig(
            max_concurrent_branches=res.get("max_concurrent_branches", 4),
            initial_branches_per_idea=res.get("initial_branches_per_idea", 2),
            max_parallel_sub_agents=res.get("max_parallel_sub_agents", 1),
            max_verify_fix_cycles=res.get("max_verify_fix_cycles", 3),
        )

        # Atomic swap — callers mid-flight see either old or new, never partial
        self._backends = backends
        self._stages = stages
        self._quant = quant
        self.resources = resources

    def reload(self) -> None:
        """Re-parse models.yaml in place. Live calls pick up the new config immediately."""
        self._load()

    def get_stage(self, stage_key: str) -> StageConfig:
        if stage_key not in self._stages:
            raise KeyError(f"Stage '{stage_key}' not found in models.yaml")
        return self._stages[stage_key]

    def get_backend(self, backend_name: str) -> BackendConfig:
        if backend_name not in self._backends:
            raise KeyError(f"Backend '{backend_name}' not found in models.yaml")
        return self._backends[backend_name]

    def all_stages(self) -> dict[str, StageConfig]:
        return dict(self._stages)
