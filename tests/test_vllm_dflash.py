"""v5 — vLLM/DFlash backend: provider request shape + serve helper command."""
from __future__ import annotations

import io
import json

import core.providers as providers


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_vllm_provider_builds_openai_request(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["auth"] = req.headers.get("Authorization")
        return _FakeResp(json.dumps({
            "choices": [{"message": {"content": "hi from vllm"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }).encode())

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    p = providers.VLLMProvider("Qwen/Qwen3.5-27B", base_url="http://localhost:8000/v1")
    resp = p.chat([{"role": "user", "content": "hi"}], options={"temperature": 0.5, "num_predict": 100})

    assert resp.content == "hi from vllm"
    assert resp.provider == "vllm"
    assert captured["url"].endswith("/chat/completions")
    assert captured["body"]["model"] == "Qwen/Qwen3.5-27B"
    assert captured["body"]["max_tokens"] == 100
    assert captured["body"]["temperature"] == 0.5
    assert captured["auth"].startswith("Bearer ")


def test_smartprovider_creates_vllm_slot():
    p = providers.SmartProvider.__new__(providers.SmartProvider)
    prov = p._create_provider({"type": "vllm", "model": "Qwen/Qwen3.5-27B", "base_url": "http://x:8000/v1"})
    assert isinstance(prov, providers.VLLMProvider)
    assert prov.model == "Qwen/Qwen3.5-27B"
    assert prov.provider_type == "vllm"


def test_serve_helper_builds_speculative_config():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "serve_vllm_dflash",
        Path(__file__).parent.parent / "scripts" / "serve_vllm_dflash.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class Args:
        model = "Qwen/Qwen3.5-27B"
        draft = None
        host = "0.0.0.0"
        port = 8000
        num_speculative_tokens = 15
        attention_backend = "flash_attn"
        max_num_batched_tokens = 32768
        api_key = ""
        extra = []

    cmd = mod.build_command(Args())
    assert "vllm" in cmd and "serve" in cmd
    i = cmd.index("--speculative-config")
    spec_obj = json.loads(cmd[i + 1])
    assert spec_obj["method"] == "dflash"
    assert spec_obj["model"] == "z-lab/Qwen3.5-27B-DFlash"
    assert spec_obj["num_speculative_tokens"] == 15


def test_serve_helper_known_drafts_present():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "serve_vllm_dflash2",
        Path(__file__).parent.parent / "scripts" / "serve_vllm_dflash.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "Qwen/Qwen3.5-27B" in mod.DFLASH_DRAFTS
    assert mod.DFLASH_DRAFTS["Qwen/Qwen3.5-27B"].startswith("z-lab/")
