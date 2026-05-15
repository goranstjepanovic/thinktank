import logging
import logging.handlers
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Logging — configure before anything else so all module-level loggers pick
# it up.  Uvicorn overrides the root logger config; we attach a stream handler
# directly to the root so application logs always reach the console.
# ---------------------------------------------------------------------------
_stream = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False)
_handler = logging.StreamHandler(_stream)
_LOG_FMT = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
_handler.setFormatter(_LOG_FMT)
logging.root.addHandler(_handler)
logging.root.setLevel(logging.INFO)


class _SizedDateRotatingHandler(logging.handlers.BaseRotatingHandler):
    """Rotate log files by size; each new file gets a datetime name; prune oldest beyond max_files."""

    def __init__(self, log_dir: Path, max_bytes: int = 10 * 1024 * 1024, max_files: int = 20):
        self._log_dir = log_dir
        self._max_bytes = max_bytes
        self._max_files = max_files
        log_dir.mkdir(parents=True, exist_ok=True)
        super().__init__(self._new_path(), mode="a", encoding="utf-8")

    def _new_path(self) -> str:
        return str(self._log_dir / datetime.now().strftime("%Y-%m-%d_%H-%M-%S.log"))

    def shouldRollover(self, record) -> int:
        if self.stream is None:
            self.stream = self._open()
        self.stream.seek(0, 2)
        return 1 if self.stream.tell() >= self._max_bytes else 0

    def doRollover(self) -> None:
        if self.stream:
            self.stream.flush()
            self.stream.close()
            self.stream = None
        self.baseFilename = self._new_path()
        self.stream = self._open()
        self._prune()

    def _prune(self) -> None:
        files = sorted(self._log_dir.glob("????-??-??_??-??-??.log"), key=lambda p: p.name)
        for p in files[: -self._max_files]:
            p.unlink(missing_ok=True)


_LOGS_DIR = Path(__file__).parent.parent / "logs"
_file_handler = _SizedDateRotatingHandler(_LOGS_DIR)
_file_handler.setFormatter(_LOG_FMT)
logging.root.addHandler(_file_handler)

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
import app.telemetry as _telemetry
import app.memory as _memory


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure data directories exist
    settings.ensure_dirs()

    # Configure model telemetry log (append-only JSONL, one record per LLM call)
    _telemetry.configure(settings.telemetry_log_path)

    # Configure agent memory store (per-project file observations + semantic retrieval)
    _memory.configure(
        db_path=settings.memory_db_path,
        embed_model=settings.embed_model,
        ollama_base_url=settings.ollama_base_url,
    )

    # Init DB tables
    await init_db()

    # Reset any sessions that were left in an active state from a previous run.
    # Background tasks are not preserved across restarts, so PLANNING/RUNNING/WAITING
    # sessions have dead background tasks and must be reset to FAILED so users can restart.
    import json as _json
    from sqlalchemy import select, update
    from app.db.models import Phase3ActivityEvent, Phase3Session
    async with AsyncSessionLocal() as _db:
        stale_r = await _db.execute(
            select(Phase3Session.id)
            .where(Phase3Session.status.in_(["PLANNING", "RUNNING", "WAITING"]))
        )
        stale_ids = [row[0] for row in stale_r.all()]

        await _db.execute(
            update(Phase3Session)
            .where(Phase3Session.status.in_(["PLANNING", "RUNNING", "WAITING"]))
            .values(status="FAILED")
        )

        # Cancel any tasks that were started or queued but never received a terminal
        # event. Scan ALL non-complete sessions — not just those reset right now —
        # so previously-stuck FAILED sessions also get cleaned up on restart.
        all_sessions_r = await _db.execute(
            select(Phase3Session.id).where(Phase3Session.status != "COMPLETE")
        )
        all_non_complete_ids = [row[0] for row in all_sessions_r.all()]

        orphaned: dict[tuple[str, str], str] = {}
        if all_non_complete_ids:
            events_r = await _db.execute(
                select(Phase3ActivityEvent.session_id, Phase3ActivityEvent.event_type, Phase3ActivityEvent.payload_json)
                .where(Phase3ActivityEvent.session_id.in_(all_non_complete_ids))
                .where(Phase3ActivityEvent.event_type.in_([
                    "sub_agent_queued", "sub_agent_started", "sub_agent_fix_started",
                    "sub_agent_complete", "sub_agent_cancelled", "sub_agent_fix_complete",
                ]))
            )
            started: dict[tuple[str, str], str] = {}
            finished: set[tuple[str, str]] = set()
            for sid, event_type, payload_json in events_r.all():
                try:
                    payload = _json.loads(payload_json)
                except Exception:
                    continue
                task_id = payload.get("task_id", "")
                if not task_id:
                    continue
                key = (sid, task_id)
                if event_type in ("sub_agent_complete", "sub_agent_cancelled", "sub_agent_fix_complete"):
                    finished.add(key)
                else:
                    started.setdefault(key, payload.get("title", task_id))

            orphaned = {k: v for k, v in started.items() if k not in finished}
            for (sid, task_id), title in orphaned.items():
                _db.add(Phase3ActivityEvent(
                    session_id=sid,
                    event_type="sub_agent_cancelled",
                    payload_json=_json.dumps({"task_id": task_id, "title": title}),
                ))

        await _db.commit()

    logging.getLogger(__name__).info(
        "startup: reset %d stale session(s) to FAILED, cancelled %d orphaned task(s)",
        len(stale_ids), len(orphaned),
    )

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
    return {stage: vars(cfg) for stage, cfg in app.state.inference_client.registry.all_stages().items()}


@app.post("/api/v1/system/models/reload")
async def reload_models():
    """Re-read models.yaml without restarting. Safe to call while pipelines are running."""
    app.state.inference_client.registry.reload()
    stages = app.state.inference_client.registry.all_stages()
    return {"reloaded": True, "stages": list(stages.keys())}
