"""Tests for inline image display:
  • generate_image returns markdown `![…](path)` in its output
  • proxy serves /data/generated/<file> with the right mime type
  • proxy refuses path traversal and forbidden subtrees of /data/
  • main system prompt instructs the model to preserve image markdown
"""
from __future__ import annotations

import io
import socket
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

from core.plugins import image_gen_tools


# ───────────────────────────────────────────────────────────────────────
# Tool-output shape
# ───────────────────────────────────────────────────────────────────────


class _OnePixelImage:
    """Minimal PIL.Image stand-in: just exposes .save(path)."""
    def __init__(self, color=(0, 255, 0)):
        self._color = color

    def save(self, path):
        # Write a tiny valid PNG (1x1 solid pixel) — enough that proxy.py
        # can serve it with a real Content-Length.
        from PIL import Image
        img = Image.new("RGB", (1, 1), self._color)
        img.save(path)


class _FakePipelineResult:
    def __init__(self, images):
        self.images = images


class _FakePipeline:
    device = "cpu"

    def set_progress_bar_config(self, disable=True):  # diffusers API
        pass

    def __call__(self, **kwargs):
        return _FakePipelineResult([_OnePixelImage()])


def _make_fake_pipeline_factory():
    """Return a fake _ensure_pipeline that bypasses diffusers loading."""
    def _fake(target):
        return _FakePipeline()
    return _fake


def test_generate_image_returns_markdown_in_output(tmp_path, monkeypatch):
    """The tool output should be a markdown image link the UI can render."""
    monkeypatch.setattr(image_gen_tools, "_ensure_pipeline", _make_fake_pipeline_factory())

    out_dir = tmp_path / "gen_out"
    result = image_gen_tools._generate_image({
        "prompt": "a misty forest at dawn",
        "width": 64,
        "height": 64,
        "steps": 1,
        "cfg_scale": 0.0,
        "output_dir": str(out_dir),
    })

    assert result.ok, f"tool failed: {result.output} | hint={result.hint}"
    assert "![Generated image:" in result.output, (
        "tool output is missing markdown image syntax — UI will display a "
        f"plain path instead. Output:\n{result.output}"
    )
    assert "a misty forest at dawn" in result.output  # alt text uses prompt
    # Forward-slash URL — required for Windows browsers.
    assert "\\" not in result.output.split("](")[1].split(")")[0]


def test_generate_image_meta_paths_still_raw(tmp_path, monkeypatch):
    """meta['output_paths'] must remain the raw filesystem path (for
    session files_touched, T2 rendering, etc.) — not the markdown link."""
    monkeypatch.setattr(image_gen_tools, "_ensure_pipeline", _make_fake_pipeline_factory())

    out_dir = tmp_path / "gen_out2"
    result = image_gen_tools._generate_image({
        "prompt": "x",
        "output_dir": str(out_dir),
    })

    assert result.ok
    paths = result.meta.get("output_paths") or []
    assert len(paths) == 1
    assert "![" not in paths[0], "meta should be the raw path, not markdown"
    assert Path(paths[0]).exists(), f"file not written at {paths[0]}"


# ───────────────────────────────────────────────────────────────────────
# Proxy static routing for /data/generated/
# ───────────────────────────────────────────────────────────────────────


@pytest.fixture
def proxy_server(tmp_path, monkeypatch):
    """Start the proxy's HTTP handler on an ephemeral port pointing at a
    temp project root with a known generated image. Yields (host, port)."""
    from core import proxy as proxy_mod

    # Lay out a fake project root that mirrors the real layout.
    project_root = tmp_path / "TheAgent0"
    (project_root / "core").mkdir(parents=True)
    (project_root / "data" / "generated").mkdir(parents=True)
    (project_root / "data" / "sessions").mkdir(parents=True)
    (project_root / "TheAgent0UI").mkdir(parents=True)
    (project_root / "TheAgent0UI" / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    # Write a small image so we have something real to fetch.
    from PIL import Image
    img_path = project_root / "data" / "generated" / "sd-test.png"
    Image.new("RGB", (4, 4), (255, 0, 0)).save(img_path)
    # And a sensitive file under sessions/ to verify it's NOT served.
    (project_root / "data" / "sessions" / "secret.jsonl").write_text(
        '{"goal": "secret"}\n', encoding="utf-8",
    )

    # Patch core/proxy.py's __file__ so its Path(__file__).parent.parent
    # resolves to the temp project root.
    original_handler = proxy_mod.ProxyHandler
    fake_module_file = str(project_root / "core" / "proxy.py")
    monkeypatch.setattr(proxy_mod, "__file__", fake_module_file)

    # Start the server on an ephemeral port in a daemon thread.
    server = proxy_mod.ThreadingHTTPServer(("127.0.0.1", 0), original_handler)
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
    headers = dict(resp.getheaders())
    conn.close()
    return resp.status, headers, body


def test_proxy_serves_data_generated(proxy_server):
    host, port = proxy_server
    status, headers, body = _get(host, port, "/data/generated/sd-test.png")
    assert status == 200, f"expected 200, got {status}; body={body[:100]!r}"
    assert headers.get("Content-Type", "").startswith("image/")
    # Should be a real PNG file (signature \x89PNG\r\n\x1a\n).
    assert body[:8] == b"\x89PNG\r\n\x1a\n", "served body is not a PNG"


def test_proxy_refuses_path_traversal(proxy_server):
    host, port = proxy_server
    # Try to escape /data/generated/ into /data/sessions/.
    status, _, _ = _get(host, port, "/data/generated/../sessions/secret.jsonl")
    assert status in (403, 404), f"path traversal not blocked: status={status}"


def test_proxy_refuses_url_encoded_traversal(proxy_server):
    host, port = proxy_server
    status, _, _ = _get(host, port, "/data/generated/%2E%2E/sessions/secret.jsonl")
    assert status in (403, 404), f"encoded traversal not blocked: status={status}"


def test_proxy_refuses_other_data_subdirs(proxy_server):
    host, port = proxy_server
    status, _, _ = _get(host, port, "/data/sessions/secret.jsonl")
    assert status in (403, 404), (
        f"/data/sessions/ was served — must be blocked. status={status}"
    )


def test_proxy_returns_404_for_missing_image(proxy_server):
    host, port = proxy_server
    status, _, _ = _get(host, port, "/data/generated/does-not-exist.png")
    assert status == 404


# ───────────────────────────────────────────────────────────────────────
# System prompt rule
# ───────────────────────────────────────────────────────────────────────


def test_system_prompt_contains_image_markdown_rule():
    from config import AGENT_PROFILES
    sp = AGENT_PROFILES["main"]["system_prompt"]
    flat = " ".join(sp.split())
    # The rule lives in the EXAMPLES block.
    assert "VERBATIM" in flat, "image-markdown VERBATIM rule missing from prompt"
    assert "render the image inline" in flat, (
        "prompt does not explain WHY the model should keep the markdown"
    )
