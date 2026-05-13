"""
Image generation tool — ComfyUI backend.

ComfyUI must be running locally (default: http://localhost:8188).
Configure COMFYUI_BASE_URL and COMFYUI_MODEL in .env.

Flow:
  1. POST /prompt with a standard txt2img workflow → get prompt_id
  2. Poll GET /history/{prompt_id} until the job completes
  3. GET /view?filename=...&type=output → download image bytes
  4. Write bytes to the resolved abs path within the project directory
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
class ImageGenerationResult:
    path: str = ""          # relative path within the project
    abs_path: str = ""
    backend: str = "comfyui"
    width: int = 0
    height: int = 0
    error: str | None = None
    duration_ms: int = 0


def _build_checkpoint_workflow(
    prompt: str,
    negative_prompt: str,
    model_name: str,
    width: int,
    height: int,
    steps: int,
    seed: int,
) -> dict:
    """Standard workflow for checkpoint models (SD1.5, SDXL, etc.)."""
    return {
        "4": {"inputs": {"ckpt_name": model_name}, "class_type": "CheckpointLoaderSimple"},
        "5": {"inputs": {"width": width, "height": height, "batch_size": 1}, "class_type": "EmptyLatentImage"},
        "6": {"inputs": {"text": prompt, "clip": ["4", 1]}, "class_type": "CLIPTextEncode"},
        "7": {"inputs": {"text": negative_prompt, "clip": ["4", 1]}, "class_type": "CLIPTextEncode"},
        "3": {
            "inputs": {
                "seed": seed, "steps": steps, "cfg": 7.0,
                "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
                "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
            "class_type": "KSampler",
        },
        "8": {"inputs": {"samples": ["3", 0], "vae": ["4", 2]}, "class_type": "VAEDecode"},
        "9": {"inputs": {"filename_prefix": "thinktank", "images": ["8", 0]}, "class_type": "SaveImage"},
    }


def _build_unet_workflow(
    prompt: str,
    unet_name: str,
    clip_name: str,
    vae_name: str,
    width: int,
    height: int,
    steps: int,
    seed: int,
) -> dict:
    """Workflow for UNET-based models (FLUX-style) with separate CLIP and VAE."""
    return {
        "1": {"inputs": {"unet_name": unet_name, "weight_dtype": "default"}, "class_type": "UNETLoader"},
        "2": {"inputs": {"clip_name": clip_name, "type": "qwen_image"}, "class_type": "CLIPLoader"},
        "3": {"inputs": {"vae_name": vae_name}, "class_type": "VAELoader"},
        "4": {"inputs": {"text": prompt, "clip": ["2", 0]}, "class_type": "CLIPTextEncode"},
        "5": {"inputs": {"width": width, "height": height, "batch_size": 1}, "class_type": "EmptyLatentImage"},
        "6": {
            "inputs": {
                "seed": seed, "steps": steps, "cfg": 1.0,
                "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
                "model": ["1", 0], "positive": ["4", 0], "negative": ["4", 0],
                "latent_image": ["5", 0],
            },
            "class_type": "KSampler",
        },
        "7": {"inputs": {"samples": ["6", 0], "vae": ["3", 0]}, "class_type": "VAEDecode"},
        "8": {"inputs": {"filename_prefix": "thinktank", "images": ["7", 0]}, "class_type": "SaveImage"},
    }


async def _discover_models(base_url: str) -> dict:
    """
    Return available models for each loader type.
    Result: {"checkpoint": [...], "unet": [...], "clip": [...], "vae": [...]}
    """
    import httpx
    result = {"checkpoint": [], "unet": [], "clip": [], "vae": []}
    queries = {
        "CheckpointLoaderSimple": ("checkpoint", "ckpt_name"),
        "UNETLoader": ("unet", "unet_name"),
        "CLIPLoader": ("clip", "clip_name"),
        "VAELoader": ("vae", "vae_name"),
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for node, (key, param) in queries.items():
                resp = await client.get(f"{base_url}/object_info/{node}")
                if resp.is_success:
                    models = (
                        resp.json().get(node, {})
                        .get("input", {}).get("required", {})
                        .get(param, [[]])[0]
                    )
                    result[key] = models if isinstance(models, list) else []
    except Exception as exc:
        logger.warning("image_generator: failed to query ComfyUI models: %s", exc)
    return result


async def _auto_detect_model(base_url: str) -> str | None:
    """Return the first available checkpoint or UNET model name."""
    discovered = await _discover_models(base_url)
    if discovered["checkpoint"]:
        return discovered["checkpoint"][0]
    if discovered["unet"]:
        return discovered["unet"][0]
    return None


async def generate_image(
    prompt: str,
    output_path: str,
    allowed_base_dir: str,
    base_url: str = "http://localhost:8188",
    model_name: str = "",
    negative_prompt: str = "ugly, blurry, low quality, watermark, text, logo",
    width: int = 512,
    height: int = 512,
    steps: int = 20,
    timeout_seconds: int = 300,
) -> ImageGenerationResult:
    import httpx
    from app.tools.path_utils import normalize_project_relative_path

    rel_path = normalize_project_relative_path(allowed_base_dir, output_path)
    if not rel_path:
        return ImageGenerationResult(error="Invalid output path")

    # Ensure path has an image extension
    if Path(rel_path).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        rel_path = rel_path + ".png"

    abs_path = str(Path(allowed_base_dir) / rel_path)
    base_url = base_url.rstrip("/")
    start = time.monotonic()

    width = max(256, min(width, 2048))
    height = max(256, min(height, 2048))
    seed = random.randint(0, 2**32 - 1)
    client_id = str(uuid.uuid4())

    discovered = await _discover_models(base_url)
    all_known = discovered["checkpoint"] + discovered["unet"]

    def _resolve_model(name: str) -> str:
        """Return the exact ComfyUI filename that best matches *name*.

        Handles common mismatches: missing .safetensors extension, hyphen/underscore
        differences, and case variations so .env values don't need exact filenames.
        """
        if name in all_known:
            return name
        # Normalise: strip extension, replace hyphens with underscores, lowercase
        def _norm(s: str) -> str:
            return s.lower().replace("-", "_").removesuffix(".safetensors").removesuffix(".ckpt").removesuffix(".pt")
        target = _norm(name)
        for candidate in all_known:
            if _norm(candidate) == target:
                logger.info("image_generator: resolved %r → %r", name, candidate)
                return candidate
        return name  # return as-is and let ComfyUI report the error

    # Resolve model name and pick the right workflow
    if not model_name:
        model_name = (discovered["checkpoint"] or discovered["unet"] or [None])[0]
        if not model_name:
            return ImageGenerationResult(
                error="No models found in ComfyUI. Download a checkpoint or UNET model first.",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        logger.info("image_generator: auto-detected model %r", model_name)
    else:
        model_name = _resolve_model(model_name)

    is_unet = model_name in discovered["unet"] or model_name not in discovered["checkpoint"]

    if is_unet:
        clip_name = (discovered["clip"] or [None])[0]
        vae_name = (discovered["vae"] or [None])[0]
        if not clip_name or not vae_name:
            return ImageGenerationResult(
                error=(
                    f"UNET model {model_name!r} requires separate CLIP and VAE models. "
                    f"Found CLIP: {clip_name!r}, VAE: {vae_name!r}. "
                    "Download the required CLIP/VAE models into ComfyUI."
                ),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        logger.info("image_generator: UNET workflow — unet=%r clip=%r vae=%r", model_name, clip_name, vae_name)
        workflow = _build_unet_workflow(prompt, model_name, clip_name, vae_name, width, height, steps, seed)
    else:
        logger.info("image_generator: checkpoint workflow — model=%r", model_name)
        workflow = _build_checkpoint_workflow(prompt, negative_prompt, model_name, width, height, steps, seed)

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            # Submit the workflow
            submit_resp = await client.post(
                f"{base_url}/prompt",
                json={"prompt": workflow, "client_id": client_id},
            )
            if not submit_resp.is_success:
                return ImageGenerationResult(
                    error=f"ComfyUI /prompt error {submit_resp.status_code}: {submit_resp.text[:300]}",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            prompt_id = submit_resp.json().get("prompt_id")
            if not prompt_id:
                return ImageGenerationResult(
                    error="ComfyUI did not return a prompt_id",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            logger.info("image_generator: submitted prompt_id=%s model=%r", prompt_id, model_name)

            # Poll history until done
            deadline = time.monotonic() + timeout_seconds
            output_filename: str | None = None
            output_subfolder: str = ""

            while time.monotonic() < deadline:
                await asyncio.sleep(_POLL_INTERVAL)
                hist_resp = await client.get(f"{base_url}/history/{prompt_id}")
                if hist_resp.is_success:
                    history = hist_resp.json()
                    if prompt_id in history:
                        for node_output in history[prompt_id].get("outputs", {}).values():
                            images = node_output.get("images", [])
                            if images:
                                output_filename = images[0]["filename"]
                                output_subfolder = images[0].get("subfolder", "")
                                break
                        if output_filename:
                            break

            if not output_filename:
                return ImageGenerationResult(
                    error=f"Image generation timed out after {timeout_seconds}s",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )

            # Download the generated image
            params: dict = {"filename": output_filename, "type": "output"}
            if output_subfolder:
                params["subfolder"] = output_subfolder
            img_resp = await client.get(f"{base_url}/view", params=params)
            if not img_resp.is_success:
                return ImageGenerationResult(
                    error=f"Failed to download image from ComfyUI: {img_resp.status_code}",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )

            Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
            Path(abs_path).write_bytes(img_resp.content)
            logger.info("image_generator: saved %r (%d B)", rel_path, len(img_resp.content))

            return ImageGenerationResult(
                path=rel_path,
                abs_path=abs_path,
                width=width,
                height=height,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

    except Exception as exc:
        return ImageGenerationResult(
            error=str(exc),
            duration_ms=int((time.monotonic() - start) * 1000),
        )
