"""Image generation via in-process Hugging Face `diffusers`.

Exposes a `generate_image` tool that loads a Stable Diffusion / SDXL
pipeline from a configurable HuggingFace model id and writes the
resulting PNG(s) into `./data/generated/`.

The pipeline is lazily loaded on the FIRST call (~30-90 s while the
weights download from HF and load onto the device) and cached
module-level for subsequent calls. Swapping `config.SD_MODEL_ID`
triggers a re-load on the next call.

Dependencies (OPTIONAL — not in requirements.txt; the tool reports a
clean install hint if they're missing):

    pip install diffusers transformers torch accelerate safetensors

For NVIDIA GPU acceleration on Windows:

    pip install torch --index-url https://download.pytorch.org/whl/cu121

Safety: `safety_checker=None` and `requires_safety_checker=False` are
deliberately set because this is a private single-user local install.
The built-in diffusers NSFW filter would otherwise blur output for
adult creative work the operator has opted into.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

_DEFAULT_SD_MODEL = "stabilityai/sdxl-turbo"
GENERATED_DIR = Path("data/generated")

# Lazy-loaded module-level cache.
_PIPELINE = None
_PIPELINE_ID: Optional[str] = None


def _sd_model_id() -> str:
    try:
        from config import SD_MODEL_ID  # type: ignore
        return (SD_MODEL_ID or "").strip() or _DEFAULT_SD_MODEL
    except Exception:
        return _DEFAULT_SD_MODEL


def _ensure_pipeline(target: str):
    """Load (or reuse) a diffusers pipeline for `target` model id.

    Raises RuntimeError with an install-hint message if diffusers/torch
    aren't installed. The handler catches that and returns a clean
    ToolResult.error so the agent can report it without crashing the loop.
    """
    global _PIPELINE, _PIPELINE_ID
    if _PIPELINE is not None and _PIPELINE_ID == target:
        return _PIPELINE

    import sys
    logger.info(
        "[generate_image] image_gen_tools running on python=%s (executable=%s); attempting diffusers+torch import",
        sys.version.split()[0], sys.executable,
    )
    try:
        from diffusers import AutoPipelineForText2Image
        import torch
    except ImportError as e:
        # Log full traceback + interpreter info so we can see WHICH import
        # failed and from WHICH Python. The user-facing RuntimeError gets
        # truncated by the UI; the log line below is the ground truth.
        logger.exception(
            "[generate_image] ImportError loading diffusers/torch from python=%s (executable=%s): %s",
            sys.version.split()[0], sys.executable, e,
        )
        raise RuntimeError(
            f"ImportError: {e} | python={sys.executable} | "
            "Run: pip install diffusers transformers torch accelerate safetensors. "
            "For NVIDIA GPU on Windows, install torch from "
            "https://download.pytorch.org/whl/cu121 first."
        ) from e

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    logger.info("[generate_image] loading %s on %s (dtype=%s)…", target, device, dtype)
    t0 = time.perf_counter()

    kwargs: Dict[str, Any] = {
        "torch_dtype": dtype,
        "safety_checker": None,
        "requires_safety_checker": False,
    }
    if device == "cuda":
        kwargs["variant"] = "fp16"

    try:
        pipe = AutoPipelineForText2Image.from_pretrained(target, **kwargs)
    except Exception:
        kwargs.pop("variant", None)
        pipe = AutoPipelineForText2Image.from_pretrained(target, **kwargs)

    pipe = pipe.to(device)
    try:
        pipe.set_progress_bar_config(disable=True)
    except Exception:
        pass

    elapsed = int((time.perf_counter() - t0) * 1000)
    logger.info("[generate_image] pipeline ready in %dms", elapsed)

    _PIPELINE = pipe
    _PIPELINE_ID = target
    return pipe


def _generate_image(args: Dict[str, Any]) -> ToolResult:
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "generate_image requires a non-empty `prompt`.",
            retryable=False,
        )

    target = (args.get("model") or _sd_model_id()).strip() or _DEFAULT_SD_MODEL

    try:
        pipe = _ensure_pipeline(target)
    except RuntimeError as e:
        return ToolResult.error(
            ErrorCode.EXTERNAL_TIMEOUT,
            str(e),
            hint="Install diffusers + torch — see README 'Image generation' section.",
            retryable=False,
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(
            ErrorCode.UNKNOWN,
            f"Failed to load pipeline for {target}: {e}",
            hint="Verify the HuggingFace model id is correct and the weights are downloadable.",
            retryable=False,
        )

    is_turbo = "turbo" in target.lower()
    is_xl = "xl" in target.lower() or "sdxl" in target.lower()
    steps_default = 4 if is_turbo else 25
    px_default = 1024 if is_xl else 512
    cfg_default = 0.0 if is_turbo else 7.0

    width = int(args.get("width", px_default))
    height = int(args.get("height", px_default))
    steps = int(args.get("steps", steps_default))
    guidance = float(args.get("cfg_scale", cfg_default))
    negative = args.get("negative_prompt", "") or None
    seed = args.get("seed")

    generator = None
    if seed is not None:
        try:
            import torch
            gen_device = "cuda" if str(pipe.device).startswith("cuda") else "cpu"
            generator = torch.Generator(device=gen_device).manual_seed(int(seed))
        except (TypeError, ValueError, ImportError):
            generator = None

    try:
        result = pipe(
            prompt=prompt,
            negative_prompt=negative,
            num_inference_steps=steps,
            guidance_scale=guidance,
            width=width,
            height=height,
            generator=generator,
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(
            ErrorCode.UNKNOWN,
            f"diffusers generation failed: {e}",
            hint="Likely VRAM exhaustion or an unsupported size — try smaller width/height or switch to a lighter model via config.SD_MODEL_ID.",
            retryable=False,
        )

    images = getattr(result, "images", None) or []
    if not images:
        return ToolResult.error(
            ErrorCode.NO_RESULTS,
            "Pipeline returned no images.",
            retryable=True,
        )

    raw_dir = args.get("output_dir")
    out_dir = Path(raw_dir) if raw_dir else GENERATED_DIR
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return ToolResult.error(
            ErrorCode.UNKNOWN, f"Could not create output dir {out_dir}: {e}", retryable=False,
        )

    ts = time.strftime("%Y%m%d-%H%M%S")
    saved: List[str] = []
    for i, img in enumerate(images):
        suffix = f"-{i}" if len(images) > 1 else ""
        path = out_dir / f"sd-{ts}{suffix}.png"
        try:
            img.save(path)
        except Exception as e:  # noqa: BLE001
            return ToolResult.error(
                ErrorCode.UNKNOWN, f"Could not write {path}: {e}", retryable=False,
            )
        saved.append(str(path))

    # Emit markdown image syntax so the chat UI renders the picture inline
    # via `marked`. Forward-slash the path so it works as a URL on both
    # Windows and POSIX. The /data/generated/ static route in proxy.py
    # serves these paths to the browser. We also keep the raw paths in
    # meta["output_paths"] so callers that need the literal filesystem
    # path (e.g. files_touched, session memory) still get it.
    def _to_url(p: str) -> str:
        u = p.replace("\\", "/")
        if u.startswith("./"):
            u = u[2:]
        return u

    alt = (prompt[:60] + ("…" if len(prompt) > 60 else "")).replace("]", "").replace("[", "")
    md_imgs = "\n\n".join(f"![Generated image: {alt}]({_to_url(p)})" for p in saved)

    return ToolResult.success(
        f"Generated {len(saved)} image(s) via {target}.\n\n{md_imgs}",
        files_touched=saved,
        output_paths=saved,
        backend="diffusers",
        model=target,
        width=width,
        height=height,
        steps=steps,
    )


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="generate_image",
        description=(
            "Generate an image from a text prompt using an in-process "
            "Hugging Face `diffusers` Stable Diffusion / SDXL pipeline. "
            f"Default model: {_DEFAULT_SD_MODEL}. Saves PNG(s) under "
            "./data/generated/ and returns the path(s). On first call, "
            "downloads ~6 GB of weights and loads them onto the GPU "
            "(~30-90 s); subsequent calls are fast. Reports a clear "
            "install-hint error if diffusers/torch aren't installed."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt":          {"type": "string",  "description": "Positive prompt — what to render."},
                "negative_prompt": {"type": "string",  "description": "What to avoid (e.g. 'blurry, low quality')."},
                "width":           {"type": "integer", "description": "Pixels. Default: 1024 for SDXL, 512 otherwise."},
                "height":          {"type": "integer", "description": "Pixels. Default: same as width."},
                "steps":           {"type": "integer", "description": "Sampling steps. Default: 4 for *-turbo, 25 otherwise."},
                "cfg_scale":       {"type": "number",  "description": "Guidance scale. Default: 0 for *-turbo, 7 otherwise."},
                "seed":            {"type": "integer", "description": "Seed for reproducibility. Omit for random."},
                "model":           {"type": "string",  "description": "Override HuggingFace model id (e.g. 'stabilityai/stable-diffusion-2-1')."},
                "output_dir":      {"type": "string",  "description": "Override save dir; default ./data/generated."},
            },
            "required": ["prompt"],
        },
        handler=_generate_image,
        requires_permission=False,
        category="image",
    ))
