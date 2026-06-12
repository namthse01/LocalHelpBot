"""hwfit_tools.py — agent tools for the Cookbook / hw-fit subsystem (v6).

Three read-only tools (no permission gate — they only probe the local box and
read Ollama's model list):

  hardware_info     — RAM / GPU / VRAM / backend + memory budgets.
  model_fit         — does <model> fit on this box? run-mode, headroom, tok/s,
                      and a copy-pasteable serve recipe.
  recommend_models  — rank the installed Ollama models by how well they fit,
                      best first.

All return a typed :class:`ToolResult`. The heavy lifting lives in
:mod:`core.hwfit`; this module is the thin agent-facing wrapper.
"""
from __future__ import annotations

from typing import Any, Dict, List

from core import hwfit
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult


def _fmt_row(a: Dict[str, Any]) -> str:
    flag = {"perfect": "✅", "good": "✅", "marginal": "⚠️",
            "too_tight": "⚠️", "no_fit": "❌"}.get(a["fit_level"], "•")
    return (f"{flag} {a['name']}  ({a['quant']}, ~{a['weights_gb']}GB) "
            f"→ {a['run_mode']}/{a['fit_level']}, ~{a['tokens_s']} tok/s "
            f"[{a['total_gb']}GB @ {a['ctx']} ctx]")


# ── hardware_info ─────────────────────────────────────────────────────────
def _hardware_info(args: Dict[str, Any]) -> ToolResult:
    try:
        hw = hwfit.probe()
        body = hwfit.hardware_report(hw)
        meta = {k: v for k, v in hw.items() if not k.startswith("_")}
        return ToolResult.success(body, **meta)
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"hardware_info failed: {e}")


# ── model_fit ──────────────────────────────────────────────────────────────
def _model_fit(args: Dict[str, Any]) -> ToolResult:
    name = (args.get("model") or args.get("name") or "").strip()
    if not name:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "model_fit requires 'model' (an installed Ollama model name).",
            retryable=False,
        )
    ctx = args.get("ctx")
    try:
        ctx = int(ctx) if ctx is not None else None
    except (TypeError, ValueError):
        return ToolResult.error(ErrorCode.INVALID_ARGS, "ctx must be an integer.", retryable=False)

    try:
        hw = hwfit.probe()
        models = hwfit.fetch_installed_models()
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"model_fit probe failed: {e}")

    match = next(
        (m for m in models if (m.get("name") or m.get("model")) == name),
        None,
    )
    if match is None:
        # Tolerate a tag-less name ("qwen2.5" → "qwen2.5:latest" etc.).
        match = next(
            (m for m in models
             if (m.get("name") or m.get("model") or "").split(":")[0] == name.split(":")[0]),
            None,
        )
    if match is None:
        available = ", ".join((m.get("name") or "?") for m in models[:12]) or "(none)"
        return ToolResult.error(
            ErrorCode.FILE_NOT_FOUND,
            f"Model {name!r} is not installed in Ollama.",
            hint=f"Installed: {available}. Run recommend_models to see what fits.",
            retryable=False,
        )

    rec = hwfit.serve_recipe(match, hw, ctx)
    a = rec["analysis"]
    body = "\n".join([
        _fmt_row(a),
        "",
        rec["recipe"],
        "",
        *(f"• {n}" for n in rec["notes"]),
    ])
    return ToolResult.success(
        body,
        model=a["name"], run_mode=a["run_mode"], fit_level=a["fit_level"],
        fits=a["fits"], total_gb=a["total_gb"], tokens_s=a["tokens_s"],
    )


# ── recommend_models ────────────────────────────────────────────────────────
def _recommend_models(args: Dict[str, Any]) -> ToolResult:
    ctx = args.get("ctx")
    try:
        ctx = int(ctx) if ctx is not None else None
    except (TypeError, ValueError):
        return ToolResult.error(ErrorCode.INVALID_ARGS, "ctx must be an integer.", retryable=False)
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    fit_only = bool(args.get("fit_only"))

    try:
        hw = hwfit.probe()
        models = hwfit.fetch_installed_models()
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"recommend_models probe failed: {e}")

    if not models:
        return ToolResult.error(
            ErrorCode.NO_RESULTS,
            "No installed Ollama models found.",
            hint="Pull one first, e.g. `ollama pull qwen2.5:7b`.",
            retryable=True,
        )

    rows: List[Dict[str, Any]] = hwfit.rank_models(hw, models, ctx)
    if fit_only:
        rows = [r for r in rows if r["fits"]]
    rows = rows[: max(limit, 1)]

    header = hwfit.hardware_report(hw)
    table = "\n".join(_fmt_row(r) for r in rows) or "(nothing fits)"
    body = f"{header}\n\nRanked installed models (best fit first):\n{table}"
    return ToolResult.success(body, count=len(rows),
                              backend=hw["backend"], ram_gb=hw["ram_gb"],
                              vram_gb=hw["vram_gb"])


# ── register ─────────────────────────────────────────────────────────────
def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="hardware_info",
        description=(
            "Probe this machine for LLM serving: RAM, GPU, VRAM, backend "
            "(cuda/rocm/metal/cpu) and the memory budgets used for fit checks. "
            "Read-only, no permission prompt."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_hardware_info,
        category="hardware",
    ))
    registry.register(Tool(
        name="model_fit",
        description=(
            "Check whether an INSTALLED Ollama model fits on this box: run-mode "
            "(gpu/cpu_offload/cpu_only/no_fit), memory headroom, a rough tokens/s, "
            "and a copy-pasteable `ollama run` recipe. Optional 'ctx' (context "
            "tokens, default 8192) changes the KV-cache estimate."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Installed Ollama model name (e.g. 'qwen2.5:7b')"},
                "ctx":   {"type": "integer", "description": "Context length in tokens (default 8192)"},
            },
            "required": ["model"],
        },
        handler=_model_fit,
        category="hardware",
    ))
    registry.register(Tool(
        name="recommend_models",
        description=(
            "Rank the installed Ollama models by how well they fit this machine "
            "(best fit first), with run-mode and rough tokens/s for each. "
            "Optional 'ctx', 'limit', and 'fit_only' (drop models that don't fit)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ctx":      {"type": "integer", "description": "Context length in tokens (default 8192)"},
                "limit":    {"type": "integer", "description": "Max rows (default 20)"},
                "fit_only": {"type": "boolean", "description": "Only show models that actually fit"},
            },
        },
        handler=_recommend_models,
        category="hardware",
    ))
