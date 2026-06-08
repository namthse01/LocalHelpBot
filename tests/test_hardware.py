"""Hardware probe -> model-tier mapping (used by install.py).

These guard the sizing logic so a fresh clone never auto-pulls weights
that won't fit the machine. Detection itself (nvidia-smi, /proc/meminfo)
is environment-specific and not unit-tested here; we test the pure
``recommend()`` mapping by feeding it synthetic hardware.
"""
from __future__ import annotations

from core.hardware import GpuInfo, Recommendation, recommend

_NO_GPU = GpuInfo(present=False)


def _rec(ram_gb: float, gpu: GpuInfo = _NO_GPU) -> Recommendation:
    return recommend(ram_gb=ram_gb, gpu=gpu, cpu_cores=4)


def test_tiny_box_gets_small_models_and_no_large():
    r = _rec(4.0)
    assert r.tier == "tiny"
    assert r.chat_model == "llama3.2:3b"
    assert r.large_model == ""          # too small to host a large model


def test_low_box_gets_7b():
    r = _rec(8.0)
    assert r.tier == "low"
    assert ":7b" in r.chat_model


def test_mid_box_gets_project_default_14b():
    r = _rec(16.0)
    assert r.tier == "mid"
    assert r.chat_model == "huihui_ai/qwen2.5-abliterate:14b"


def test_high_box_gets_32b_and_a_large_model():
    r = _rec(64.0)
    assert r.tier == "high"
    assert ":32b" in r.chat_model
    assert r.large_model                # high tier ships a large model


def test_nvidia_vram_overrides_low_ram():
    # 8 GB system RAM but a 24 GB GPU -> sizing should follow VRAM, not RAM.
    big_gpu = GpuInfo(present=True, vendor="nvidia", name="RTX", vram_gb=24.0)
    r = _rec(8.0, gpu=big_gpu)
    assert r.tier == "high"
    assert ":32b" in r.chat_model


def test_apple_silicon_falls_back_to_ram_budget():
    apple = GpuInfo(present=True, vendor="apple", name="Apple Silicon")
    r = _rec(16.0, gpu=apple)
    assert r.tier == "mid"              # shared memory -> RAM drives sizing


def test_pull_list_is_deduped_and_skips_empty_large():
    r = _rec(16.0)
    pl = r.pull_list()
    assert len(pl) == len(set(pl))      # no duplicates
    assert "" not in pl
    assert r.chat_model in pl and r.embed_model in pl
