"""v6 — proxy routes behind the Hardware (Cookbook / hw-fit) UI tab.

  • GET  /api/hwfit            → {hardware, report, models[ranked]}
  • POST /api/hwfit/recipe     → {ok, recipe, analysis, notes}

Reuses the proxy-server harness from tests/test_image_inline.py (fake project
root, ``core.proxy.__file__`` patched, an ephemeral ThreadingHTTPServer). The
hardware probe and the Ollama catalogue are monkeypatched on ``core.hwfit`` so
nothing here touches the GPU or the network.
"""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest

from core.hardware import GpuInfo


def _hw():
    return {
        "ram_gb": 32.0, "vram_gb": 12.0, "gpu_present": True,
        "gpu_vendor": "nvidia", "gpu_name": "RTX 3060", "backend": "cuda",
        "gpu_budget_gb": 12.0, "ram_budget_gb": 32.0,
        "_gpu": GpuInfo(present=True, vendor="nvidia", name="RTX 3060", vram_gb=12.0),
    }


def _model(name, gb, params="7.6B", quant="Q4_K_M"):
    return {"name": name, "size": int(gb * (1024 ** 3)),
            "details": {"parameter_size": params, "quantization_level": quant}}


_MODELS = [
    _model("small:3b", 2.0, params="3B"),
    _model("qwen2.5:7b", 4.7),
    _model("huge:70b", 40.0, params="70B"),   # won't fit
]


@pytest.fixture
def proxy_server(tmp_path, monkeypatch):
    from core import proxy as proxy_mod
    from core import hwfit

    project_root = tmp_path / "TheAgent0"
    (project_root / "core").mkdir(parents=True)
    (project_root / "TheAgent0UI").mkdir(parents=True)
    (project_root / "TheAgent0UI" / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    monkeypatch.setattr(proxy_mod, "__file__", str(project_root / "core" / "proxy.py"))
    # Deterministic, offline probe + catalogue.
    monkeypatch.setattr(hwfit, "probe", lambda: _hw())
    monkeypatch.setattr(hwfit, "fetch_installed_models", lambda *a, **k: list(_MODELS))

    server = proxy_mod.ThreadingHTTPServer(("127.0.0.1", 0), proxy_mod.ProxyHandler)
    host, port = server.server_address
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield host, port
    finally:
        server.shutdown()
        server.server_close()


def _get(host, port, path):
    conn = HTTPConnection(host, port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, json.loads(body) if body else {}


def _post(host, port, path, obj):
    conn = HTTPConnection(host, port, timeout=10)
    conn.request("POST", path, body=json.dumps(obj).encode(),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, json.loads(body) if body else {}


# ── GET /api/hwfit ───────────────────────────────────────────────────────
def test_hwfit_ranks_models(proxy_server):
    host, port = proxy_server
    status, body = _get(host, port, "/api/hwfit")
    assert status == 200
    assert body["hardware"]["backend"] == "cuda"
    assert "_gpu" not in body["hardware"]            # internal key stripped
    assert "Backend" in body["report"]
    names = [m["name"] for m in body["models"]]
    assert set(names) == {"small:3b", "qwen2.5:7b", "huge:70b"}
    assert names[-1] == "huge:70b"                   # non-fitting last


def test_hwfit_fit_only(proxy_server):
    host, port = proxy_server
    status, body = _get(host, port, "/api/hwfit?fit_only=1")
    assert status == 200
    names = [m["name"] for m in body["models"]]
    assert "huge:70b" not in names and "qwen2.5:7b" in names


def test_hwfit_ctx_param_changes_estimate(proxy_server):
    host, port = proxy_server
    _, small_ctx = _get(host, port, "/api/hwfit?ctx=2048")
    _, big_ctx = _get(host, port, "/api/hwfit?ctx=32768")
    a = next(m for m in small_ctx["models"] if m["name"] == "qwen2.5:7b")
    b = next(m for m in big_ctx["models"] if m["name"] == "qwen2.5:7b")
    assert b["total_gb"] > a["total_gb"]             # more ctx → more KV cache


# ── POST /api/hwfit/recipe ────────────────────────────────────────────────
def test_recipe_ok(proxy_server):
    host, port = proxy_server
    status, body = _post(host, port, "/api/hwfit/recipe", {"model": "qwen2.5:7b"})
    assert status == 200 and body["ok"] is True
    assert "ollama run qwen2.5:7b" in body["recipe"]
    assert body["analysis"]["run_mode"] == "gpu"
    assert isinstance(body["notes"], list)


def test_recipe_tagless(proxy_server):
    host, port = proxy_server
    status, body = _post(host, port, "/api/hwfit/recipe", {"model": "qwen2.5"})
    assert status == 200 and body["ok"] is True
    assert body["model"] == "qwen2.5:7b"


def test_recipe_missing_model_is_404(proxy_server):
    host, port = proxy_server
    status, body = _post(host, port, "/api/hwfit/recipe", {"model": "nope:1b"})
    assert status == 404 and body["ok"] is False


def test_recipe_requires_model(proxy_server):
    host, port = proxy_server
    status, body = _post(host, port, "/api/hwfit/recipe", {})
    assert status == 400 and body["ok"] is False
