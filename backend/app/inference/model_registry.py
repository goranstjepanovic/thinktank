from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class StageConfig:
    model: str
    backend: str
    temperature: float
    max_tokens: int | None
    format: str
    num_ctx: int = 8192
    supports_tools: bool = False
    fallback_models: list[str] = field(default_factory=list)
    timeout_seconds: int | None = None  # overrides backend default when set
    extra: dict = field(default_factory=dict)  # passed verbatim to the backend payload


@dataclass
class BackendConfig:
    base_url: str | None
    timeout_seconds: int
    extra: dict


@dataclass
class ResourceConfig:
    max_concurrent_branches: int
    initial_branches_per_idea: int
    max_parallel_sub_agents: int = 1


class ModelRegistry:
    def __init__(self, yaml_path: Path) -> None:
        with open(yaml_path) as f:
            data = yaml.safe_load(f)

        self._defaults = data.get("defaults", {})
        self._backends: dict[str, BackendConfig] = {}
        self._stages: dict[str, StageConfig] = {}

        for name, cfg in data.get("backends", {}).items():
            self._backends[name] = BackendConfig(
                base_url=cfg.get("base_url"),
                timeout_seconds=cfg.get("timeout_seconds", 120),
                extra={k: v for k, v in cfg.items() if k not in ("base_url", "timeout_seconds")},
            )

        _KNOWN = {"model", "backend", "temperature", "max_tokens", "format",
                  "num_ctx", "supports_tools", "fallback_models", "timeout_seconds"}
        for stage_key, cfg in data.get("stages", {}).items():
            self._stages[stage_key] = StageConfig(
                model=cfg["model"],
                backend=cfg["backend"],
                temperature=cfg.get("temperature", self._defaults.get("temperature", 0.2)),
                max_tokens=cfg.get("max_tokens", self._defaults.get("max_tokens")),
                format=cfg.get("format", self._defaults.get("format", "json")),
                num_ctx=cfg.get("num_ctx", self._defaults.get("num_ctx", 8192)),
                supports_tools=cfg.get("supports_tools", self._defaults.get("supports_tools", False)),
                fallback_models=list(cfg.get("fallback_models") or []),
                timeout_seconds=cfg.get("timeout_seconds"),
                extra={k: v for k, v in cfg.items() if k not in _KNOWN},
            )

        res = data.get("resources", {})
        self.resources = ResourceConfig(
            max_concurrent_branches=res.get("max_concurrent_branches", 4),
            initial_branches_per_idea=res.get("initial_branches_per_idea", 2),
            max_parallel_sub_agents=res.get("max_parallel_sub_agents", 1),
        )

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
