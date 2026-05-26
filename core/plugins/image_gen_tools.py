"""Image generation via local AUTOMATIC1111 Stable Diffusion WebUI.

Exposes a `generate_image` tool that POSTs prompts to /sdapi/v1/txt2img
on the configured A1111 endpoint (default http://127.0.0.1:7860) and
writes the resulting PNG into ./data/generated/.

A1111 must be running with `--api` for this tool to succeed; otherwise
the tool returns a clear ToolResult.error pointing at the install
walkthrough in the README.

Endpoint can be overridden via `config.A1111_BASE` if the user runs A1111
on a different host/port. We resolve it lazily so a missing config attr
just falls back to the default.

Mirrors the urllib POST style used by core/plugins/vision_tools.py.
"""
from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

_DEFAULT_A1111_BASE = "http://127.0.0.1:7860"
GENERATED_DIR = Path("data/generated")

DEFAULTS = {
    "steps": 20,
    "width": 512,
    "height": 512,
    "cfg_scale": 7.0,
    "sampler_name": "Euler a",
    "negative_prompt": "",
}
TIMEOUT_SEC = 300  # SD generation can take a while on CPU; generous cap


def _a1111_base() -> str:
    try:
        from config import A1111_BASE  # type: ignore
        return (A1111_BASE or "").strip() or _DEFAULT_A1111_BASE
    except Exception:
        return _DEFAULT_A1111_BASE


def _generate_image(args: Dict[str, Any]) -> ToolResult:
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "generate_image requires a non-empty `prompt`.",
            retryable=False,
        )

    payload = {
        "prompt":         prompt,
        "negative_prompt": args.get("negative_prompt", DEFAULTS["negative_prompt"]),
        "steps":          int(args.get("steps",     DEFAULTS["steps"])),
        "width":          int(args.get("width",     DEFAULTS["width"])),
        "height":         int(args.get("height",    DEFAULTS["height"])),
        "cfg_scale":      float(args.get("cfg_scale", DEFAULTS["cfg_scale"])),
        "sampler_name":   args.get("sampler_name", DEFAULTS["sampler_name"]),
    }
    if "seed" in args:
        try:
            payload["seed"] = int(args["seed"])
        except (TypeError, ValueError):
            pass

    base = _a1111_base().rstrip("/")
    url = f"{base}/sdapi/v1/txt2img"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            err_body = ""
        return ToolResult.error(
            ErrorCode.UNKNOWN,
            f"HTTP {e.code} from A1111: {err_body}",
            hint="Check the A1111 console for the actual error and verify a model checkpoint is loaded.",
            retryable=False,
        )
    except urllib.error.URLError as e:
        return ToolResult.error(
            ErrorCode.EXTERNAL_TIMEOUT,
            f"AUTOMATIC1111 not reachable at {base}: {e}.",
            hint=(
                "Start A1111 with the --api flag. On Windows: edit webui-user.bat, "
                "set COMMANDLINE_ARGS=--api, then run it. See README \"Image generation\" section."
            ),
            retryable=True,
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(
            ErrorCode.UNKNOWN, f"A1111 request failed: {e}", retryable=False,
        )

    images_b64: List[str] = body.get("images") or []
    if not images_b64:
        return ToolResult.error(
            ErrorCode.NO_RESULTS,
            "A1111 returned no images.",
            hint="The endpoint replied but with an empty `images` array — check the A1111 console for warnings.",
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
    for i, b64 in enumerate(images_b64):
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        try:
            png = base64.b64decode(b64)
        except (ValueError, TypeError) as e:
            logger.warning("generate_image: skipping malformed b64 chunk %d: %s", i, e)
            continue
        suffix = f"-{i}" if len(images_b64) > 1 else ""
        path = out_dir / f"sd-{ts}{suffix}.png"
        try:
            path.write_bytes(png)
        except OSError as e:
            return ToolResult.error(
                ErrorCode.UNKNOWN, f"Could not write {path}: {e}", retryable=False,
            )
        saved.append(str(path))

    if not saved:
        return ToolResult.error(
            ErrorCode.UNKNOWN, "A1111 returned data but none of it decoded into images.",
            retryable=True,
        )

    return ToolResult.success(
        f"Generated {len(saved)} image(s):\n" + "\n".join(saved),
        files_touched=saved,
        output_paths=saved,
        prompt=prompt,
        width=payload["width"],
        height=payload["height"],
        steps=payload["steps"],
    )


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="generate_image",
        description=(
            "Generate an image from a text prompt using a local AUTOMATIC1111 "
            "Stable Diffusion WebUI (default http://127.0.0.1:7860, must be "
            "running with --api). Saves PNG(s) under ./data/generated/ and "
            "returns the path(s). Reports a clear error if A1111 is not running."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt":          {"type": "string",  "description": "Positive prompt — what to render."},
                "negative_prompt": {"type": "string",  "description": "What to avoid (e.g. 'blurry, low quality')."},
                "width":           {"type": "integer", "description": "Pixels, default 512. Common: 512, 768, 1024."},
                "height":          {"type": "integer", "description": "Pixels, default 512."},
                "steps":           {"type": "integer", "description": "Sampling steps, default 20. Higher = slower, finer."},
                "cfg_scale":       {"type": "number",  "description": "Guidance scale, default 7. Range 1-20."},
                "sampler_name":    {"type": "string",  "description": "Sampler name, default 'Euler a'."},
                "seed":            {"type": "integer", "description": "Seed for reproducibility. -1 (default) is random."},
                "output_dir":      {"type": "string",  "description": "Override save dir; default ./data/generated."},
            },
            "required": ["prompt"],
        },
        handler=_generate_image,
        requires_permission=False,
        category="image",
    ))
