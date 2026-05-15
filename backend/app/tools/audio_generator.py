"""
Audio generation tool — ComfyUI backend (ACE-Step 1.5).

Covers music and sound-effect generation.  Speech (TTS) is a separate concern
handled by generate_audio_speech which returns a clear not-configured error
until a TTS backend is wired up.

Flow (music / SFX):
  1. Discover ACE-Step model filenames from ComfyUI's /object_info endpoints.
  2. POST /prompt with an ACE-Step workflow → get prompt_id.
  3. Poll GET /history/{prompt_id} until the job completes.
  4. GET /view?filename=...&type=output → download audio bytes (FLAC).
  5. Write bytes to the resolved abs path within the project directory.
"""
import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 2.0


@dataclass
class AudioGenerationResult:
    path: str = ""
    abs_path: str = ""
    backend: str = "comfyui"
    duration_seconds: float = 0.0
    error: str | None = None
    duration_ms: int = 0


def _build_acestep_workflow(
    tags: str,
    duration: float,
    seed: int,
    unet_name: str,
    clip_name: str,
    vae_name: str,
) -> dict:
    """ACE-Step 1.5 ComfyUI workflow — tags-only (no lyrics)."""
    return {
        "1": {
            "inputs": {"unet_name": unet_name, "weight_dtype": "default"},
            "class_type": "UNETLoader",
        },
        "2": {
            "inputs": {
                "clip_name1": clip_name,
                "clip_name2": clip_name,  # same model for both slots; lyrics unused
                "type": "ace",
                "device": "default",
            },
            "class_type": "DualCLIPLoader",
        },
        "3": {"inputs": {"vae_name": vae_name}, "class_type": "VAELoader"},
        "4": {
            "inputs": {
                "clip": ["2", 0],
                "tags": tags,
                "lyrics": "",
                "seed": seed,
                "bpm": 120,
                "duration": duration,
                "timesignature": "4",
                "language": "en",
                "keyscale": "E minor",
                "generate_audio_codes": True,
                "cfg_scale": 2.0,
                "temperature": 0.85,
                "top_p": 0.9,
                "top_k": 0,
                "min_p": 0.0,
            },
            "class_type": "TextEncodeAceStepAudio1.5",
        },
        "5": {
            "inputs": {"seconds": duration, "batch_size": 1},
            "class_type": "EmptyAceStep1.5LatentAudio",
        },
        "6": {"inputs": {"conditioning": ["4", 0]}, "class_type": "ConditioningZeroOut"},
        "7": {"inputs": {"model": ["1", 0], "shift": 3.0}, "class_type": "ModelSamplingAuraFlow"},
        "8": {
            "inputs": {
                "model": ["7", 0],
                "positive": ["4", 0],
                "negative": ["6", 0],
                "latent_image": ["5", 0],
                "seed": seed,
                "steps": 8,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
            },
            "class_type": "KSampler",
        },
        "9": {"inputs": {"samples": ["8", 0], "vae": ["3", 0]}, "class_type": "VAEDecodeAudio"},
        "10": {
            "inputs": {"audio": ["9", 0], "filename_prefix": "thinktank_audio"},
            "class_type": "SaveAudio",
        },
    }


async def _discover_acestep_models(base_url: str) -> dict[str, str | None]:
    """Return {unet, clip, vae} ACE-Step model filenames available in ComfyUI."""
    import httpx

    result: dict[str, str | None] = {"unet": None, "clip": None, "vae": None}
    queries = [
        ("UNETLoader",    "unet_name",  "unet",  lambda m: "acestep" in m.lower()),
        ("DualCLIPLoader","clip_name1", "clip",  lambda m: "ace" in m.lower()),
        ("VAELoader",     "vae_name",   "vae",   lambda m: "ace" in m.lower()),
    ]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for node, param, key, match in queries:
                resp = await client.get(f"{base_url}/object_info/{node}")
                if resp.is_success:
                    models = (
                        resp.json().get(node, {})
                        .get("input", {}).get("required", {})
                        .get(param, [[]])[0]
                    )
                    for m in (models if isinstance(models, list) else []):
                        if match(m):
                            result[key] = m
                            break
    except Exception as exc:
        logger.warning("audio_generator: failed to query ComfyUI: %s", exc)
    return result


async def generate_audio(
    tags: str,
    output_path: str,
    allowed_base_dir: str,
    base_url: str = "http://localhost:8188",
    duration: float = 30.0,
    timeout_seconds: int = 600,
) -> AudioGenerationResult:
    """Generate music or SFX using ACE-Step 1.5 and save to the project directory."""
    import httpx
    from app.tools.path_utils import normalize_project_relative_path

    rel_path = normalize_project_relative_path(allowed_base_dir, output_path)
    if not rel_path:
        return AudioGenerationResult(error="Invalid output path")

    if Path(rel_path).suffix.lower() not in (".flac", ".wav", ".mp3"):
        rel_path = rel_path + ".flac"

    abs_path = str(Path(allowed_base_dir) / rel_path)
    base_url = base_url.rstrip("/")
    start = time.monotonic()
    duration = max(1.0, min(float(duration), 300.0))
    seed = random.randint(0, 2**32 - 1)
    client_id = str(uuid.uuid4())

    models = await _discover_acestep_models(base_url)
    missing = [k for k, v in models.items() if not v]
    if missing:
        return AudioGenerationResult(
            error=(
                f"ACE-Step models not found in ComfyUI (missing: {', '.join(missing)}). "
                "Ensure acestep_v1.5_turbo.safetensors, qwen_0.6b_ace15.safetensors, "
                "and ace_1.5_vae.safetensors are downloaded into the ComfyUI models directory."
            ),
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    workflow = _build_acestep_workflow(
        tags=tags,
        duration=duration,
        seed=seed,
        unet_name=models["unet"],
        clip_name=models["clip"],
        vae_name=models["vae"],
    )

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            submit = await client.post(
                f"{base_url}/prompt",
                json={"prompt": workflow, "client_id": client_id},
            )
            if not submit.is_success:
                return AudioGenerationResult(
                    error=f"ComfyUI /prompt error {submit.status_code}: {submit.text[:300]}",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            prompt_id = submit.json().get("prompt_id")
            if not prompt_id:
                return AudioGenerationResult(
                    error="ComfyUI did not return a prompt_id",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            logger.info("audio_generator: submitted prompt_id=%s duration=%.1fs tags=%r", prompt_id, duration, tags[:80])

            deadline = time.monotonic() + timeout_seconds
            output_filename: str | None = None
            output_subfolder: str = ""

            while time.monotonic() < deadline:
                await asyncio.sleep(_POLL_INTERVAL)
                hist = await client.get(f"{base_url}/history/{prompt_id}")
                if hist.is_success:
                    data = hist.json()
                    if prompt_id in data:
                        for node_out in data[prompt_id].get("outputs", {}).values():
                            audio_files = node_out.get("audio", [])
                            if audio_files:
                                output_filename = audio_files[0]["filename"]
                                output_subfolder = audio_files[0].get("subfolder", "")
                                break
                        if output_filename:
                            break

            if not output_filename:
                return AudioGenerationResult(
                    error=f"Audio generation timed out after {timeout_seconds}s",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )

            params: dict = {"filename": output_filename, "type": "output"}
            if output_subfolder:
                params["subfolder"] = output_subfolder
            dl = await client.get(f"{base_url}/view", params=params)
            if not dl.is_success:
                return AudioGenerationResult(
                    error=f"Failed to download audio from ComfyUI: {dl.status_code}",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )

            Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
            Path(abs_path).write_bytes(dl.content)
            logger.info("audio_generator: saved %r (%d B)", rel_path, len(dl.content))

            return AudioGenerationResult(
                path=rel_path,
                abs_path=abs_path,
                duration_seconds=duration,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

    except Exception as exc:
        return AudioGenerationResult(
            error=str(exc),
            duration_ms=int((time.monotonic() - start) * 1000),
        )
