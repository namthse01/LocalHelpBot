"""Multimodal vision tools — Slice 5 of the v4 upgrade.

  describe_image            — send a local image (base64) to Ollama's
                              vision model (default `llava`) and return
                              the description.
  screenshot_and_describe    — convenience: take a screenshot, then call
                              describe_image on it in one tool call.

Both gracefully degrade when no vision model is installed: the
ToolResult carries a clear hint and `LLAVA` healthcheck flags it.

Vision model name is configurable via `config.VISION_MODEL` (default
`llava:latest`).
"""
from __future__ import annotations

import base64
import json
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from core.permissions import request_permission
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult


_DEFAULT_VISION_MODEL = "llava:latest"
_DEFAULT_PROMPT = "Describe this image in detail. Be specific about UI elements, error messages, text, and visual cues."


def _vision_model() -> str:
    try:
        from config import VISION_MODEL   # type: ignore
        return (VISION_MODEL or "").strip() or _DEFAULT_VISION_MODEL
    except Exception:
        return _DEFAULT_VISION_MODEL


def _ollama_base() -> str:
    try:
        from config import OLLAMA_BASE   # type: ignore
        return OLLAMA_BASE
    except Exception:
        return "http://localhost:11434"


# ───────────────────────────────────────────────────────────────────────
# describe_image
# ───────────────────────────────────────────────────────────────────────


def _describe_image(args: Dict[str, Any]) -> ToolResult:
    path = (args.get("path") or "").strip()
    prompt = (args.get("prompt") or _DEFAULT_PROMPT).strip()
    model = (args.get("model") or _vision_model()).strip() or _DEFAULT_VISION_MODEL

    if not path:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "describe_image requires 'path'.", retryable=False)
    p = Path(path)
    if not p.exists():
        return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"Image not found: {path}")
    suffix = p.suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            f"Unsupported image format: {suffix}. Use png/jpg/webp/gif/bmp.",
            retryable=False,
        )

    try:
        raw = p.read_bytes()
        if len(raw) > 20 * 1024 * 1024:
            return ToolResult.error(
                ErrorCode.TOO_LARGE,
                f"Image too large ({len(raw)} bytes; max 20 MB).",
                hint="Resize or crop the image first.",
                retryable=False,
            )
        b64 = base64.b64encode(raw).decode("ascii")
    except OSError as e:
        return ToolResult.error(ErrorCode.UNKNOWN, f"Could not read image: {e}")

    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "images": [b64],
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        _ollama_base() + "/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        response_text = (data.get("response") or "").strip()
        if not response_text:
            return ToolResult.error(ErrorCode.NO_RESULTS, f"Vision model {model} returned empty output.")
        return ToolResult.success(
            f"[image: {p.name}, model: {model}]\n\n{response_text}",
            model=model, path=str(p), prompt=prompt, bytes=len(raw),
        )
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            err_body = ""
        if e.code == 404 or "not found" in err_body.lower():
            return ToolResult.error(
                ErrorCode.FILE_NOT_FOUND,
                f"Vision model '{model}' not installed in Ollama.",
                hint=f"Run `ollama pull {model.split(':')[0]}` to install it.",
                retryable=False,
            )
        return ToolResult.error(ErrorCode.UNKNOWN, f"HTTP {e.code} from Ollama: {err_body}")
    except urllib.error.URLError as e:
        return ToolResult.error(ErrorCode.EXTERNAL_TIMEOUT, f"Ollama unreachable: {e}")
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"describe_image failed: {e}")


# ───────────────────────────────────────────────────────────────────────
# screenshot_and_describe
# ───────────────────────────────────────────────────────────────────────


def _screenshot_and_describe(args: Dict[str, Any]) -> ToolResult:
    """Convenience: take a screenshot to a temp file, then describe it."""
    prompt = (args.get("prompt") or _DEFAULT_PROMPT).strip()
    model = (args.get("model") or _vision_model()).strip() or _DEFAULT_VISION_MODEL
    region = args.get("region")

    decision = request_permission(
        "screenshot", "(screen → vision model)",
        {"action": "screenshot+describe", "model": model, "region": region},
    )
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined screenshot_and_describe ({decision['reason']}).",
            retryable=False,
        )

    try:
        from PIL import ImageGrab  # type: ignore
    except ImportError:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "Pillow not installed.",
            hint="install_package name='Pillow' to enable screenshots.",
            retryable=False,
        )
    try:
        import tempfile
        bbox = None
        if region:
            try:
                parts = [int(x.strip()) for x in str(region).split(",")]
                if len(parts) == 4:
                    x, y, w, h = parts
                    bbox = (x, y, x + w, y + h)
            except Exception:
                pass
        img = ImageGrab.grab(bbox=bbox) if bbox is not None else ImageGrab.grab()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
        img.save(tmp_path, format="PNG")
        # Reuse describe_image so the vision-call branch stays single-pathed.
        result = _describe_image({"path": tmp_path, "prompt": prompt, "model": model})
        # Clean up the temp file regardless of outcome.
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass
        return result
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"screenshot_and_describe failed: {e}")


# ───────────────────────────────────────────────────────────────────────
# Register
# ───────────────────────────────────────────────────────────────────────


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="describe_image",
        description=(
            "Send a local image (PNG/JPG/WEBP/GIF/BMP) to Ollama's vision "
            "model (default llava) and return a detailed description. "
            "Override `model` or `prompt` to tune behaviour."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path":   {"type": "string", "description": "Path to the image file."},
                "prompt": {"type": "string", "description": "Optional override of the description prompt."},
                "model":  {"type": "string", "description": "Override vision model (default `llava:latest` or config.VISION_MODEL)."},
            },
            "required": ["path"],
        },
        handler=_describe_image,
        category="vision",
    ))
    registry.register(Tool(
        name="screenshot_and_describe",
        description=(
            "One-shot: take a screenshot then describe it via the vision "
            "model. Returns the description directly so the main agent can "
            "reason about what's on screen. Asks user permission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "model":  {"type": "string"},
                "region": {"type": "string", "description": "Optional 'x,y,w,h' region."},
            },
        },
        handler=_screenshot_and_describe,
        requires_permission=True,
        category="vision",
    ))
