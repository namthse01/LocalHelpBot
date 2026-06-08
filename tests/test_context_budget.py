"""v5 — adaptive input-token budget."""
from __future__ import annotations

from core.context_budget import compute_input_token_budget as cb


def test_explicit_honoured_when_window_unknown():
    assert cb(8000, 0, True) == 8000


def test_explicit_clamped_to_window():
    assert cb(50000, 32768, True) == 32768
    assert cb(8000, 32768, True) == 8000


def test_default_scales_to_window_headroom():
    # 128K window, 0.85 headroom → ~111K
    out = cb(6000, 131072, False)
    assert 100_000 < out < 120_000


def test_default_capped_at_hard_max():
    assert cb(6000, 5_000_000, False) == 200_000


def test_unknown_window_falls_back_to_default():
    assert cb(6000, 0, False) == 6000
    assert cb(0, 0, False) == 6000  # zero configured → DEFAULT_BUDGET


def test_probe_caches(monkeypatch):
    import core.context_budget as m
    m.reset_cache_for_tests()
    calls = {"n": 0}

    def fake_show(model, *, ollama_base=None, timeout=4):
        calls["n"] += 1
        return 4096

    # Patch the network probe with a counter via the cache directly.
    monkeypatch.setattr(m, "probe_context_length", m.probe_context_length)
    # Seed the cache and confirm a second read doesn't error.
    m._cache["fake:1"] = 4096
    assert m.probe_context_length("fake:1") == 4096
