import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Logging — configure before anything else so all module-level loggers pick
# it up.  Uvicorn overrides the root logger config; we attach a stream handler
# directly to the root so application logs always reach the console.
# ---------------------------------------------------------------------------
_stream = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False)
_handler = logging.StreamHandler(_stream)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
logging.root.addHandler(_handler)
logging.root.setLevel(logging.INFO)
# Quiet down noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

from app.api.router import api_router
from app.api.ws import router as ws_router
from app.config import settings
from app.db.engine import init_db, AsyncSessionLocal
from app.inference.drivers.llamacpp_driver import LlamaCppDriver
from app.inference.drivers.ollama_driver import OllamaDriver
from app.inference.drivers.openvino_driver import OpenVinoDriver
from app.inference.client import InferenceClient
from app.inference.model_registry import ModelRegistry
from app.pipeline.orchestrator import orchestrator


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure data directories exist
    settings.ensure_dirs()

    # Init DB tables
    await init_db()

    # Reset any sessions that were left in an active state from a previous run.
    # Background tasks are not preserved across restarts, so PLANNING/RUNNING/WAITING
    # sessions have dead background tasks and must be reset to DONE so users can interact.
    from sqlalchemy import update
    from app.db.models import Phase3Session
    async with AsyncSessionLocal() as _db:
        await _db.execute(
            update(Phase3Session)
            .where(Phase3Session.status.in_(["PLANNING", "RUNNING", "WAITING"]))
            .values(status="DONE")
        )
        await _db.commit()
    logging.getLogger(__name__).info("startup: reset stale PLANNING/RUNNING/WAITING sessions to DONE")

    # Load model registry
    registry = ModelRegistry(settings.models_yaml_path)

    # Build backend drivers
    ollama_cfg = registry.get_backend("ollama")
    llamacpp_cfg = registry.get_backend("llamacpp")
    openvino_cfg = registry.get_backend("openvino")

    drivers = {
        "ollama": OllamaDriver(
            base_url=ollama_cfg.base_url or settings.ollama_base_url,
            timeout_seconds=ollama_cfg.timeout_seconds,
        ),
        "llamacpp": LlamaCppDriver(
            base_url=llamacpp_cfg.base_url or settings.llamacpp_base_url,
            timeout_seconds=llamacpp_cfg.timeout_seconds,
        ),
        "openvino": OpenVinoDriver(
            model_dir=openvino_cfg.extra.get("model_dir", "C:/models/openvino"),
            device=openvino_cfg.extra.get("device", "NPU"),
            timeout_seconds=openvino_cfg.timeout_seconds,
        ),
    }

    inference_client = InferenceClient(registry=registry, drivers=drivers)

    # Wire orchestrator
    orchestrator.setup(inference_client)

    # Make inference_client available to background tasks (phase2, etc.)
    app.state.inference_client = inference_client

    yield

    # Shutdown — cancel any running pipelines
    for idea_id in list(orchestrator._active.keys()):
        await orchestrator.abandon_idea(idea_id)


app = FastAPI(title="Think Tank", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(ws_router)


def get_inference_client() -> "InferenceClient":
    """Accessor for background tasks that run outside of request context."""
    return app.state.inference_client


@app.get("/api/v1/health")
async def health():
    from app.inference.drivers.ollama_driver import OllamaDriver
    ollama = OllamaDriver(base_url=settings.ollama_base_url)
    ollama_ok = await ollama.health_check()
    models = await ollama.list_available_models() if ollama_ok else []
    return {
        "status": "ok",
        "ollama_reachable": ollama_ok,
        "ollama_models": models,
    }


@app.get("/api/v1/system/models")
async def system_models():
    registry = ModelRegistry(settings.models_yaml_path)
    return {stage: vars(cfg) for stage, cfg in registry.all_stages().items()}
