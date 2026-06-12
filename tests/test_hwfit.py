"""v6 — Cookbook / hw-fit: fit math, probe, catalogue, and agent tools.

The fit math is deterministic, so most tests pin exact numbers. Hardware
detection and the Ollama catalogue are mocked (monkeypatched on the
:mod:`core.hwfit` module) so nothing here touches the GPU or the network.
"""
from __future__ import annotations

import json

import pytest

from core import hwfit
from core.hardware import GpuInfo
from core.plugins import hwfit_tools as tools
from core.tool_schema import ErrorCode


# ── shared fake hardware ─────────────────────────────────────────────────
def _hw_gpu(vram=12.0, ram=32.0, name="RTX 3060"):
    return {
        "ram_gb": ram, "vram_gb": vram, "gpu_present": True,
        "gpu_vendor": "nvidia", "gpu_name": name, "backend": "cuda",
        "gpu_budget_gb": vram, "ram_budget_gb": ram,
        "_gpu": GpuInfo(present=True, vendor="nvidia", name=name, vram_gb=vram),
    }


def _hw_cpu(ram=16.0):
    return {
        "ram_gb": ram, "vram_gb": 0.0, "gpu_present": False,
        "gpu_vendor": "", "gpu_name": "", "backend": "cpu",
        "gpu_budget_gb": 0.0, "ram_budget_gb": ram,
        "_gpu": GpuInfo(),
    }


def _model(name, gb, params="7.6B", quant="Q4_K_M"):
    return {"name": name, "size": int(gb * (1024 ** 3)),
            "details": {"parameter_size": params, "quantization_level": quant}}


# ── params_b parsing ─────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("7.6B", 7.6), ("32.8B", 32.8), ("3B", 3.0), ("1.5B", 1.5),
    ("270M", 0.27), ("500K", 0.0005), (7_000_000_000, 7.0), (7.0, 7.0),
    ("", 0.0), (None, 0.0), ("garbage", 0.0),
])
def test_params_b(raw, expected):
    assert hwfit.params_b(raw) == pytest.approx(expected, rel=1e-3)


# ── bytes-per-parameter ──────────────────────────────────────────────────
@pytest.mark.parametrize("quant,bpp", [
    ("Q4_K_M", 0.50), ("Q5_K_M", 0.625), ("Q6_K", 0.75), ("Q8_0", 1.0),
    ("F16", 2.0), ("fp16", 2.0), ("BF16", 2.0),
    ("", 0.50), ("totally-unknown", 0.50), ("Q4", 0.50),
])
def test_bpp(quant, bpp):
    assert hwfit._bpp(quant) == bpp


# ── estimate_memory_gb ───────────────────────────────────────────────────
def test_estimate_memory_gb():
    # 4.0 GB weights + KV(7B,8192) + 0.5 overhead
    kv = 0.000008 * 7.0 * 8192
    assert hwfit.estimate_memory_gb(4.0, 7.0, 8192) == pytest.approx(4.0 + kv + 0.5, abs=0.01)


def test_estimate_memory_zero_ctx_is_weights_plus_overhead():
    assert hwfit.estimate_memory_gb(4.0, 7.0, 0) == pytest.approx(4.5)


# ── analyze_model: run modes ─────────────────────────────────────────────
def test_analyze_fits_on_gpu():
    a = hwfit.analyze_model(_model("qwen2.5:7b", 4.7), _hw_gpu(), 8192)
    assert a["run_mode"] == "gpu" and a["fits"] and a["fit_level"] == "perfect"
    assert a["weights_gb"] == 4.7 and a["tokens_s"] > 0


def test_analyze_cpu_offload_when_too_big_for_vram():
    a = hwfit.analyze_model(_model("big:20b", 20.0, params="20B"), _hw_gpu(), 8192)
    assert a["run_mode"] == "cpu_offload" and a["fits"]


def test_analyze_cpu_only_without_gpu():
    a = hwfit.analyze_model(_model("qwen2.5:7b", 4.7), _hw_cpu(16.0), 8192)
    assert a["run_mode"] == "cpu_only" and a["fits"]
    assert a["tokens_s"] == pytest.approx(hwfit.FALLBACK_BW["cpu"] / 4.7, rel=0.01)


def test_analyze_no_fit_when_bigger_than_ram():
    a = hwfit.analyze_model(_model("huge:70b", 40.0, params="70B"), _hw_gpu(12.0, 32.0), 8192)
    assert a["run_mode"] == "no_fit" and not a["fits"]
    assert a["fit_level"] == "no_fit" and a["tokens_s"] == 0.0


# ── analyze_model: fit-level headroom bands ──────────────────────────────
def test_fit_levels_track_headroom():
    hw = _hw_gpu(vram=24.0, ram=64.0)
    # perfect (<=0.6*24=14.4), good (<=19.2), marginal (<=22.8), too_tight (<=24)
    perfect = hwfit.analyze_model(_model("a", 8.0), hw, 0)
    good = hwfit.analyze_model(_model("b", 17.0), hw, 0)
    marginal = hwfit.analyze_model(_model("c", 21.0), hw, 0)
    tight = hwfit.analyze_model(_model("d", 23.2), hw, 0)
    assert perfect["fit_level"] == "perfect"
    assert good["fit_level"] == "good"
    assert marginal["fit_level"] == "marginal"
    assert tight["fit_level"] == "too_tight"


# ── analyze_model: derive params from on-disk size when label missing ────
def test_params_derived_from_size_when_label_blank():
    m = {"name": "x", "size": int(4.0 * 1024 ** 3),
         "details": {"quantization_level": "Q4_K_M"}}  # no parameter_size
    a = hwfit.analyze_model(m, _hw_gpu(), 0)
    # 4 GB / 0.5 bpp ≈ 8B
    assert a["params_b"] == pytest.approx(8.0, rel=0.05)


def test_weights_estimated_when_no_size():
    m = {"name": "x", "details": {"parameter_size": "7B", "quantization_level": "Q4_K_M"}}
    a = hwfit.analyze_model(m, _hw_gpu(), 0)
    assert a["weights_gb"] == pytest.approx(7.0 * 0.5, rel=0.01)  # 3.5


# ── ranking ──────────────────────────────────────────────────────────────
def test_rank_orders_fit_then_speed():
    hw = _hw_gpu(vram=12.0, ram=32.0)
    models = [
        _model("huge:70b", 40.0, params="70B"),   # no_fit
        _model("mid:14b", 9.0, params="14B"),      # fits, slower
        _model("small:3b", 2.0, params="3B"),      # fits, faster
    ]
    ranked = hwfit.rank_models(hw, models, 8192)
    names = [r["name"] for r in ranked]
    assert names[-1] == "huge:70b"             # non-fitting sinks to the bottom
    assert names.index("small:3b") < names.index("mid:14b")  # faster first


def test_rank_empty():
    assert hwfit.rank_models(_hw_cpu(), [], 8192) == []


# ── probe (hardware detection mocked) ────────────────────────────────────
def test_probe_nvidia(monkeypatch):
    monkeypatch.setattr(hwfit, "total_ram_gb", lambda: 32.0)
    monkeypatch.setattr(hwfit, "detect_gpu",
                        lambda: GpuInfo(present=True, vendor="nvidia", name="RTX 4090", vram_gb=24.0))
    hw = hwfit.probe()
    assert hw["backend"] == "cuda" and hw["gpu_budget_gb"] == 24.0
    assert hw["ram_budget_gb"] == 32.0


def test_probe_cpu_only(monkeypatch):
    monkeypatch.setattr(hwfit, "total_ram_gb", lambda: 16.0)
    monkeypatch.setattr(hwfit, "detect_gpu", lambda: GpuInfo(present=False))
    hw = hwfit.probe()
    assert hw["backend"] == "cpu" and hw["gpu_budget_gb"] == 0.0


def test_probe_apple_unified(monkeypatch):
    monkeypatch.setattr(hwfit, "total_ram_gb", lambda: 16.0)
    monkeypatch.setattr(hwfit, "detect_gpu",
                        lambda: GpuInfo(present=True, vendor="apple", name="Apple Silicon (Metal)"))
    hw = hwfit.probe()
    assert hw["backend"] == "metal"
    assert hw["gpu_budget_gb"] == pytest.approx(12.0)  # 0.75 * 16


# ── fetch_installed_models (network mocked) ──────────────────────────────
class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_filters_zero_size(monkeypatch):
    payload = {"models": [
        {"name": "real:7b", "size": 4_000_000_000, "details": {}},
        {"name": "virtual", "size": 0, "details": {}},   # virtual model → dropped
    ]}
    monkeypatch.setattr(hwfit.urllib.request, "urlopen",
                        lambda url, timeout=5: _FakeResp(payload))
    out = hwfit.fetch_installed_models()
    assert [m["name"] for m in out] == ["real:7b"]


def test_fetch_returns_empty_on_error(monkeypatch):
    def boom(url, timeout=5):
        raise OSError("no ollama")
    monkeypatch.setattr(hwfit.urllib.request, "urlopen", boom)
    assert hwfit.fetch_installed_models() == []


# ── serve_recipe ─────────────────────────────────────────────────────────
def test_serve_recipe_gpu():
    rec = hwfit.serve_recipe(_model("qwen2.5:7b", 4.7), _hw_gpu(), 8192)
    assert rec["model"] == "qwen2.5:7b"
    assert "ollama run qwen2.5:7b" in rec["recipe"]
    assert rec["analysis"]["run_mode"] == "gpu"
    assert any("VRAM" in n for n in rec["notes"])


def test_serve_recipe_no_fit_warns():
    rec = hwfit.serve_recipe(_model("huge:70b", 40.0, params="70B"), _hw_gpu(12.0, 32.0), 8192)
    assert any("does not fit" in n.lower() for n in rec["notes"])


# ── tools ─────────────────────────────────────────────────────────────────
def test_tool_hardware_info(monkeypatch):
    monkeypatch.setattr(hwfit, "probe", lambda: _hw_gpu())
    r = tools._hardware_info({})
    assert r.ok and "Backend" in r.output
    assert r.meta.get("backend") == "cuda" and "_gpu" not in r.meta


def test_tool_model_fit_found(monkeypatch):
    monkeypatch.setattr(hwfit, "probe", lambda: _hw_gpu())
    monkeypatch.setattr(hwfit, "fetch_installed_models",
                        lambda *a, **k: [_model("qwen2.5:7b", 4.7)])
    r = tools._model_fit({"model": "qwen2.5:7b"})
    assert r.ok and r.meta.get("run_mode") == "gpu"
    assert "ollama run qwen2.5:7b" in r.output


def test_tool_model_fit_tagless_match(monkeypatch):
    monkeypatch.setattr(hwfit, "probe", lambda: _hw_gpu())
    monkeypatch.setattr(hwfit, "fetch_installed_models",
                        lambda *a, **k: [_model("qwen2.5:7b", 4.7)])
    r = tools._model_fit({"model": "qwen2.5"})  # no tag → matches qwen2.5:7b
    assert r.ok and r.meta.get("model") == "qwen2.5:7b"


def test_tool_model_fit_missing(monkeypatch):
    monkeypatch.setattr(hwfit, "probe", lambda: _hw_gpu())
    monkeypatch.setattr(hwfit, "fetch_installed_models", lambda *a, **k: [])
    r = tools._model_fit({"model": "nope:1b"})
    assert not r.ok and r.error_code is ErrorCode.FILE_NOT_FOUND


def test_tool_model_fit_requires_model():
    r = tools._model_fit({})
    assert not r.ok and r.error_code is ErrorCode.INVALID_ARGS


def test_tool_recommend(monkeypatch):
    monkeypatch.setattr(hwfit, "probe", lambda: _hw_gpu())
    monkeypatch.setattr(hwfit, "fetch_installed_models",
                        lambda *a, **k: [_model("small:3b", 2.0, params="3B"),
                                         _model("huge:70b", 40.0, params="70B")])
    r = tools._recommend_models({})
    assert r.ok and r.meta.get("count") == 2
    # fit_only drops the non-fitting one
    r2 = tools._recommend_models({"fit_only": True})
    assert r2.meta.get("count") == 1


def test_tool_recommend_no_models(monkeypatch):
    monkeypatch.setattr(hwfit, "probe", lambda: _hw_cpu())
    monkeypatch.setattr(hwfit, "fetch_installed_models", lambda *a, **k: [])
    r = tools._recommend_models({})
    assert not r.ok and r.error_code is ErrorCode.NO_RESULTS
