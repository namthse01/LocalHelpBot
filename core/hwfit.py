"""hwfit.py — "will this model run on my box?" fit analysis.

Ported (and trimmed) from odysseus ``services/hwfit`` and adapted to
TheAgent0: synchronous, stdlib-only, single-user/local. We DROP odysseus's
SSH/remote-host probing and its download/serve lifecycle — those are out of
scope. What's left is the genuinely portable core:

  • hardware probe   — reuses :mod:`core.hardware` (RAM + GPU/VRAM detect),
                       wrapped into a budget dict (VRAM vs RAM, backend).
  • memory estimate  — quant byte tables + KV-cache term + overhead. Unlike
                       odysseus we PREFER the real on-disk weight size that
                       Ollama already reports, falling back to the estimate
                       only when a model carries no ``size``.
  • fit + speed      — run-mode (gpu / cpu_offload / cpu_only / no_fit),
                       fit-level headroom, and a rough bandwidth-bound
                       tokens/s using a GPU-bandwidth table.
  • ranking          — analyse every installed Ollama model and sort by how
                       well it fits, best first.
  • serve recipe     — a copy-pasteable ``ollama run`` recipe (+ a vLLM hint
                       when the box is clearly big enough) for one model.

Everything here is best-effort and never raises for the caller: a failed
probe degrades to a CPU/RAM-only view, an unparseable model is skipped.
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Dict, List, Optional

from core.hardware import GpuInfo, detect_gpu, total_ram_gb


# ── tunables (read defensively so tests can monkeypatch config) ──────────
def _cfg(name: str, default: Any) -> Any:
    try:
        import config  # local import: avoids a hard import cycle at module load

        return getattr(config, name, default)
    except Exception:
        return default


def _ollama_base() -> str:
    return str(_cfg("OLLAMA_BASE", "http://localhost:11434")).rstrip("/")


def _default_ctx() -> int:
    try:
        return int(_cfg("HWFIT_DEFAULT_CTX", 8192))
    except Exception:
        return 8192


GIB = 1024 ** 3
OVERHEAD_GB = 0.5          # runtime / framework overhead on top of weights+KV
KV_PER_PARAM_B_PER_TOK = 0.000008   # GB of KV cache per (billion params · token)


# ── quantisation → bytes-per-parameter ───────────────────────────────────
# Keys are upper-cased; lookup is substring-tolerant via :func:`_bpp`.
QUANT_BYTES_PER_PARAM: Dict[str, float] = {
    "Q2_K": 0.33,
    "Q3_K_S": 0.39, "Q3_K_M": 0.43, "Q3_K_L": 0.47, "Q3_K": 0.43,
    "Q4_0": 0.50, "Q4_1": 0.5625, "Q4_K_S": 0.50, "Q4_K_M": 0.50, "Q4_K": 0.50,
    "Q5_0": 0.625, "Q5_1": 0.6875, "Q5_K_S": 0.625, "Q5_K_M": 0.625, "Q5_K": 0.625,
    "Q6_K": 0.75,
    "Q8_0": 1.00, "Q8_K": 1.00,
    "F16": 2.00, "FP16": 2.00, "BF16": 2.00, "F32": 4.00, "FP32": 4.00,
    "FP8": 1.00, "INT8": 1.00, "AWQ": 0.50, "GPTQ": 0.50,
    "FP4": 0.50, "INT4": 0.50, "MXFP4": 0.50,
}
_DEFAULT_BPP = 0.50  # assume ~Q4 when we genuinely can't tell


def _bpp(quant: str) -> float:
    """Bytes-per-parameter for a quant label, tolerant of odd formatting."""
    if not quant:
        return _DEFAULT_BPP
    key = re.sub(r"[^A-Z0-9_]", "", quant.upper())
    if key in QUANT_BYTES_PER_PARAM:
        return QUANT_BYTES_PER_PARAM[key]
    # Substring fallback: longest keys first so "Q4_K_M" wins over "Q4".
    for k in sorted(QUANT_BYTES_PER_PARAM, key=len, reverse=True):
        if k in key:
            return QUANT_BYTES_PER_PARAM[k]
    return _DEFAULT_BPP


# ── GPU memory bandwidth (GB/s) for a rough tokens/s estimate ────────────
# Decode is bandwidth-bound: each generated token reads ~all weights once, so
# tokens/s ≈ bandwidth / weights_GB. Keys matched as lowercase substrings,
# longest first ("m1 ultra" before "m1").
GPU_BANDWIDTH: Dict[str, float] = {
    "h100": 3350, "a100": 2039, "a6000": 768, "a5000": 768, "a4000": 448,
    "l40": 864, "l4": 300, "v100": 900, "p100": 732, "t4": 320,
    "rtx 5090": 1792, "rtx 4090": 1008, "rtx 4080": 717, "rtx 4070 ti": 504,
    "rtx 4070": 504, "rtx 4060 ti": 288, "rtx 4060": 272,
    "rtx 3090 ti": 1008, "rtx 3090": 936, "rtx 3080 ti": 912, "rtx 3080": 760,
    "rtx 3070": 448, "rtx 3060 ti": 448, "rtx 3060": 360,
    "rtx 2080 ti": 616, "rtx 2080": 448, "rtx 2070": 448, "rtx 2060": 336,
    "rx 7900 xtx": 960, "rx 7900": 800, "rx 6900": 512, "rx 6800": 512,
    "m1 ultra": 800, "m1 max": 400, "m1 pro": 200, "m1": 68,
    "m2 ultra": 800, "m2 max": 400, "m2 pro": 200, "m2": 100,
    "m3 ultra": 800, "m3 max": 400, "m3 pro": 150, "m3": 100,
    "m4 max": 546, "m4 pro": 273, "m4": 120,
}
# Per-backend fallback bandwidth when the GPU name isn't in the table.
FALLBACK_BW: Dict[str, float] = {
    "cuda": 400.0, "rocm": 320.0, "metal": 200.0, "cpu": 50.0,
}


def _bandwidth_for(gpu: GpuInfo, backend: str) -> float:
    name = (gpu.name or "").lower()
    for key in sorted(GPU_BANDWIDTH, key=len, reverse=True):
        if key in name:
            return GPU_BANDWIDTH[key]
    return FALLBACK_BW.get(backend, FALLBACK_BW["cpu"])


# ── parameter-count parsing ──────────────────────────────────────────────
def params_b(parameter_size: Any) -> float:
    """Parse a parameter-size label to billions of params.

    Accepts ``"7.6B"`` / ``"32.8B"`` / ``"270M"`` / ``"7000000000"`` / a raw
    number. Returns ``0.0`` when it can't tell.
    """
    if parameter_size is None:
        return 0.0
    if isinstance(parameter_size, (int, float)):
        v = float(parameter_size)
        return v / 1e9 if v > 1e6 else v  # raw count vs already-in-billions
    s = str(parameter_size).strip().upper().replace(",", "")
    if not s:
        return 0.0
    m = re.match(r"([0-9]*\.?[0-9]+)\s*([BMK]?)", s)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "B":
        return val
    if unit == "M":
        return val / 1000.0
    if unit == "K":
        return val / 1_000_000.0
    # No unit: a big bare number is a raw count, a small one is already billions.
    return val / 1e9 if val > 1e6 else val


# ── per-model fit analysis ───────────────────────────────────────────────
def estimate_memory_gb(weights_gb: float, params_billion: float, ctx: int) -> float:
    """weights + KV-cache(ctx) + fixed overhead, in GiB."""
    kv = KV_PER_PARAM_B_PER_TOK * max(params_billion, 0.0) * max(ctx, 0)
    return round(weights_gb + kv + OVERHEAD_GB, 2)


def probe() -> Dict[str, Any]:
    """Detect the local machine → a budget dict the fit math runs against.

    Reuses :mod:`core.hardware`. The *budget* is VRAM when a discrete NVIDIA
    GPU is present (that's what most people actually run on); otherwise system
    RAM. Apple Silicon shares memory, so RAM is the budget but the backend is
    ``metal``.
    """
    ram = total_ram_gb()
    gpu = detect_gpu()

    if gpu.present and gpu.vendor == "nvidia":
        backend = "cuda"
    elif gpu.present and gpu.vendor == "amd":
        backend = "rocm"
    elif gpu.present and gpu.vendor == "apple":
        backend = "metal"
    else:
        backend = "cpu"

    vram = gpu.vram_gb if (gpu.present and gpu.vram_gb > 0) else 0.0
    # The "fits fully on the accelerator" budget.
    if backend == "cuda" and vram > 0:
        gpu_budget = vram
    elif backend in ("metal", "rocm"):
        # Unified / shared memory: most of RAM is usable by the GPU.
        gpu_budget = round(ram * 0.75, 1)
    else:
        gpu_budget = 0.0

    return {
        "ram_gb": ram,
        "vram_gb": vram,
        "gpu_present": gpu.present,
        "gpu_vendor": gpu.vendor,
        "gpu_name": gpu.name,
        "backend": backend,
        "gpu_budget_gb": gpu_budget,   # fits-fully-on-GPU budget
        "ram_budget_gb": ram,          # CPU-offload ceiling
        "_gpu": gpu,                   # internal: for bandwidth lookup
    }


# Fit-level thresholds as a fraction of the effective budget.
_FIT_THRESHOLDS = (
    (0.60, "perfect"),
    (0.80, "good"),
    (0.95, "marginal"),
    (1.00, "too_tight"),
)


def analyze_model(model: Dict[str, Any], hw: Dict[str, Any],
                  ctx: Optional[int] = None) -> Dict[str, Any]:
    """Analyse one Ollama model dict (`/api/tags` shape) against ``hw``."""
    ctx = _default_ctx() if ctx is None else int(ctx)
    name = model.get("name") or model.get("model") or "?"
    details = model.get("details") or {}
    quant = str(details.get("quantization_level") or "").strip()
    pb = params_b(details.get("parameter_size"))

    size_bytes = int(model.get("size") or 0)
    if size_bytes > 0:
        weights_gb = round(size_bytes / GIB, 2)
        if pb <= 0:                      # back out a param count from the file
            pb = round((size_bytes / GIB) / max(_bpp(quant), 0.01), 1)
    else:
        weights_gb = round(pb * _bpp(quant), 2)

    total_gb = estimate_memory_gb(weights_gb, pb, ctx)

    gpu_budget = float(hw.get("gpu_budget_gb") or 0.0)
    ram_budget = float(hw.get("ram_budget_gb") or 0.0)
    backend = hw.get("backend") or "cpu"
    has_gpu = bool(hw.get("gpu_present")) and gpu_budget > 0

    # Run mode + the budget the fit-level is measured against.
    if has_gpu and total_gb <= gpu_budget:
        run_mode = "gpu"
        eff_budget = gpu_budget
    elif total_gb <= ram_budget:
        run_mode = "cpu_offload" if has_gpu else "cpu_only"
        eff_budget = ram_budget
    else:
        run_mode = "no_fit"
        eff_budget = ram_budget

    ratio = total_gb / eff_budget if eff_budget > 0 else 99.0
    fit_level = "no_fit"
    if run_mode != "no_fit":
        fit_level = "too_tight"
        for thresh, level in _FIT_THRESHOLDS:
            if ratio <= thresh:
                fit_level = level
                break

    tokens_s = _estimate_tokens_s(weights_gb, run_mode, hw)

    return {
        "name": name,
        "params_b": round(pb, 2),
        "quant": quant or "?",
        "weights_gb": weights_gb,
        "ctx": ctx,
        "total_gb": total_gb,
        "run_mode": run_mode,
        "fit_level": fit_level,
        "fits": run_mode != "no_fit",
        "budget_gb": round(eff_budget, 1),
        "headroom_gb": round(eff_budget - total_gb, 2),
        "tokens_s": tokens_s,
    }


def _estimate_tokens_s(weights_gb: float, run_mode: str,
                       hw: Dict[str, Any]) -> float:
    """Very rough bandwidth-bound decode speed (tokens/s)."""
    if weights_gb <= 0:
        return 0.0
    gpu = hw.get("_gpu") or GpuInfo()
    backend = hw.get("backend") or "cpu"
    if run_mode == "gpu":
        bw = _bandwidth_for(gpu, backend)
    elif run_mode == "cpu_offload":
        # Part on GPU, part spills to (slow) system RAM → blended, GPU-weighted.
        bw = 0.5 * _bandwidth_for(gpu, backend) + 0.5 * FALLBACK_BW["cpu"]
    elif run_mode == "cpu_only":
        bw = FALLBACK_BW["cpu"]
    else:
        return 0.0
    return round(bw / weights_gb, 1)


# ── installed-model catalogue + ranking ──────────────────────────────────
def fetch_installed_models(timeout: float = 5.0) -> List[Dict[str, Any]]:
    """Real Ollama models from ``OLLAMA_BASE/api/tags`` (size>0 only).

    Hits Ollama directly (port 11434), NOT TheAgent0's proxy — so the virtual
    models (which report ``size: 0``) never show up here. Never raises.
    """
    try:
        with urllib.request.urlopen(_ollama_base() + "/api/tags", timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for m in data.get("models", []):
        if int(m.get("size") or 0) > 0:
            out.append(m)
    return out


# Sort key: fitting models first, then better fit, then faster.
_FIT_RANK = {"perfect": 0, "good": 1, "marginal": 2, "too_tight": 3, "no_fit": 4}


def rank_models(hw: Dict[str, Any], models: List[Dict[str, Any]],
                ctx: Optional[int] = None) -> List[Dict[str, Any]]:
    """Analyse every model and sort best-fit first. Pure — pass models in."""
    rows = [analyze_model(m, hw, ctx) for m in models]
    rows.sort(key=lambda r: (
        0 if r["fits"] else 1,
        _FIT_RANK.get(r["fit_level"], 9),
        -r["tokens_s"],
    ))
    return rows


# ── serve recipe ─────────────────────────────────────────────────────────
def serve_recipe(model: Dict[str, Any], hw: Dict[str, Any],
                 ctx: Optional[int] = None) -> Dict[str, Any]:
    """A copy-pasteable run recipe + human notes for one model."""
    a = analyze_model(model, hw, ctx)
    name = a["name"]
    ctx = a["ctx"]
    lines: List[str] = []
    notes: List[str] = []

    lines.append(f"# {name}  ·  {a['quant']}  ·  ~{a['weights_gb']} GB weights")
    lines.append(f"# total ~{a['total_gb']} GB @ {ctx} ctx → "
                 f"{a['run_mode']} ({a['fit_level']}), ~{a['tokens_s']} tok/s")
    lines.append("")
    lines.append(f"OLLAMA_NUM_PARALLEL=1 ollama run {name}")
    lines.append(f'#   set context:  /set parameter num_ctx {ctx}')

    if a["run_mode"] == "gpu":
        notes.append("Fits fully in VRAM — full GPU offload, fastest path.")
    elif a["run_mode"] == "cpu_offload":
        notes.append(
            "Too big for VRAM — Ollama will split layers GPU↔CPU automatically. "
            "Lower num_ctx or pick a smaller quant to push more onto the GPU."
        )
    elif a["run_mode"] == "cpu_only":
        notes.append("No usable GPU — running on CPU; expect the tok/s above.")
    else:
        notes.append(
            f"Does NOT fit: needs ~{a['total_gb']} GB but the box only offers "
            f"~{a['budget_gb']} GB. Use a smaller model or a heavier quant."
        )

    # vLLM hint only when the box is clearly a CUDA machine with real headroom.
    if hw.get("backend") == "cuda" and a["run_mode"] == "gpu" and a["fits"]:
        notes.append(
            "Big-GPU option: serve via vLLM for higher throughput "
            "(see scripts/serve_vllm_dflash.py / VLLMProvider)."
        )

    return {"model": name, "analysis": a, "recipe": "\n".join(lines), "notes": notes}


# ── human-readable hardware summary (for the hardware_info tool / UI) ─────
def hardware_report(hw: Optional[Dict[str, Any]] = None) -> str:
    hw = probe() if hw is None else hw
    bar = "-" * 44
    gpu_line = hw.get("gpu_name") or "none"
    if hw.get("vram_gb"):
        gpu_line += f" — {hw['vram_gb']:.0f} GB VRAM"
    lines = [
        "TheAgent0 — hardware fit probe",
        bar,
        f"RAM        : {hw['ram_gb']:.0f} GB",
        f"GPU        : {gpu_line}",
        f"Backend    : {hw['backend']}",
        f"GPU budget : {hw['gpu_budget_gb']:.0f} GB (fits-fully-on-GPU)",
        f"RAM budget : {hw['ram_budget_gb']:.0f} GB (CPU-offload ceiling)",
        bar,
    ]
    return "\n".join(lines)
