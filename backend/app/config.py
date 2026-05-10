from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Paths
    base_dir: Path = Path(__file__).parent.parent
    data_dir: Path = base_dir / "data"
    documents_dir: Path = data_dir / "documents"
    implementations_dir: Path = data_dir / "implementations"
    models_yaml_path: Path = base_dir / "models.yaml"
    telemetry_log_path: Path = base_dir / "logs" / "model_telemetry.jsonl"

    # Database
    database_url: str = "sqlite+aiosqlite:///./data/thinktank.db"
    database_url_sync: str = "sqlite:///./data/thinktank.db"  # for Alembic

    # Inference backends
    ollama_base_url: str = "http://localhost:11434"
    llamacpp_base_url: str = "http://localhost:8080"

    # Web search
    # Tavily (optional, better quality) — free tier 1 000 req/month: https://app.tavily.com
    # If not set, DuckDuckGo is used automatically (no key required).
    tavily_api_key: str = ""

    # Image generation (ComfyUI — local, free)
    # Start ComfyUI first, then set these in .env.
    # COMFYUI_MODEL: checkpoint filename as it appears in ComfyUI (e.g. "sd_xl_base_1.0.safetensors").
    # Leave empty to auto-detect the first available model.
    comfyui_base_url: str = "http://localhost:8188"
    comfyui_model: str = ""

    # Pipeline
    max_concurrent_branches: int = 4
    initial_branches_per_idea: int = 2
    max_branches_per_idea: int = 6  # hard cap — prevents runaway spawning

    # Script runner (F16) — sandboxed Python, short timeout
    script_runner_timeout_seconds: int = 30
    script_runner_max_output_kb: int = 64

    # Shell runner (F19) — unrestricted shell for Phase 3 agents
    shell_runner_timeout_seconds: int = 300   # npm install / pip install can take a while
    shell_runner_max_output_kb: int = 64

    # Server — CORS is open (*) since this runs local-only with no auth

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        self.implementations_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
