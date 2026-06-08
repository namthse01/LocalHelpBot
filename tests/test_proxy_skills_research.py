"""Polish (v6) — proxy routes behind the Skills + Research UI tabs.

  • GET  /api/skills          → learned skills as JSON (most-used first)
  • POST /api/skills/delete   → remove a skill by slug ({ok: bool})
  • GET  /api/research        → saved Deep-Research reports as JSON
  • GET  /data/research/<f>   → serve a report (html/md/txt only), guarded

Mirrors the proxy-server harness in tests/test_image_inline.py: a fake project
root, ``core.proxy.__file__`` patched so the static guard resolves there, and
an ephemeral ThreadingHTTPServer. No network, no Ollama.
"""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest


@pytest.fixture
def proxy_server(tmp_path, monkeypatch):
    from core import proxy as proxy_mod
    from core import skills as sk
    from core import deep_research as dr

    # Fake project root mirroring the real layout.
    project_root = tmp_path / "TheAgent0"
    (project_root / "core").mkdir(parents=True)
    (project_root / "data" / "generated").mkdir(parents=True)
    (project_root / "data" / "research").mkdir(parents=True)
    (project_root / "data" / "sessions").mkdir(parents=True)
    (project_root / "TheAgent0UI").mkdir(parents=True)
    (project_root / "TheAgent0UI" / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    # A sensitive file that must never be reachable via /data/research/..
    (project_root / "data" / "sessions" / "secret.jsonl").write_text(
        '{"goal": "secret"}\n', encoding="utf-8",
    )

    # Isolate the skills store singleton to the temp tree.
    store = sk.reset_skills_store_for_tests(root=project_root / "data" / "skills")

    # Point deep_research's RESEARCH_DIR at the temp tree so the GET /api/research
    # route and the /data/research/ static route agree on where reports live.
    research_dir = project_root / "data" / "research"
    monkeypatch.setattr(dr, "RESEARCH_DIR", research_dir)

    # A sample report: HTML (with a <title>) + markdown sibling.
    (research_dir / "20260608-101010-test-report.html").write_text(
        "<!doctype html><title>How to test things</title><h1>How to test things</h1>",
        encoding="utf-8",
    )
    (research_dir / "20260608-101010-test-report.md").write_text(
        "# How to test things\n", encoding="utf-8",
    )

    # Patch __file__ so Path(__file__).resolve().parent.parent → temp root.
    monkeypatch.setattr(proxy_mod, "__file__", str(project_root / "core" / "proxy.py"))

    server = proxy_mod.ThreadingHTTPServer(("127.0.0.1", 0), proxy_mod.ProxyHandler)
    host, port = server.server_address
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield host, port, store, research_dir
    finally:
        server.shutdown()
        server.server_close()
        sk.reset_skills_store_for_tests()  # restore default singleton


def _get(host, port, path):
    conn = HTTPConnection(host, port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def _post(host, port, path, obj):
    conn = HTTPConnection(host, port, timeout=10)
    conn.request("POST", path, body=json.dumps(obj).encode(),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


# ── /api/skills ─────────────────────────────────────────────────────────
def test_api_skills_empty(proxy_server):
    host, port, _store, _ = proxy_server
    status, body = _get(host, port, "/api/skills")
    assert status == 200
    assert json.loads(body)["skills"] == []


def test_api_skills_lists_vietnamese_slug(proxy_server):
    host, port, store, _ = proxy_server
    store.add(title="Cách tạo tệp mới", when_to_use="khi cần tạo file",
              procedure=["mo terminal", "touch file"], tags=["vn", "fs"])
    status, body = _get(host, port, "/api/skills")
    assert status == 200
    skills = json.loads(body)["skills"]
    # VN diacritics transliterate to a readable ASCII slug (the polish fix).
    assert [s["name"] for s in skills] == ["cach-tao-tep-moi"]
    assert skills[0]["tags"] == ["vn", "fs"]


def test_api_skills_delete_removes(proxy_server):
    host, port, store, _ = proxy_server
    store.add(title="Deploy app", procedure=["build", "push"])
    status, body = _post(host, port, "/api/skills/delete", {"name": "deploy-app"})
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert store.get("deploy-app") is None


def test_api_skills_delete_missing_is_404(proxy_server):
    host, port, _store, _ = proxy_server
    status, body = _post(host, port, "/api/skills/delete", {"name": "nope"})
    assert status == 404
    assert json.loads(body)["ok"] is False


# ── /api/research + /data/research/ ──────────────────────────────────────
def test_api_research_lists_report(proxy_server):
    host, port, _store, _ = proxy_server
    status, body = _get(host, port, "/api/research")
    assert status == 200
    reports = json.loads(body)["reports"]
    assert len(reports) == 1
    rep = reports[0]
    assert rep["title"] == "How to test things"      # parsed from <title>
    assert rep["file"].endswith(".html")
    assert rep["url"] == "/data/research/" + rep["file"]
    assert rep["md_url"] and rep["md_url"].endswith(".md")
    assert rep["size"] > 0


def test_data_research_serves_html(proxy_server):
    host, port, _store, _ = proxy_server
    status, body = _get(host, port, "/data/research/20260608-101010-test-report.html")
    assert status == 200
    assert b"How to test things" in body


def test_data_research_rejects_disallowed_extension(proxy_server):
    host, port, _store, research_dir = proxy_server
    # A .py dropped into the reports dir must NOT be served (ext allow-list).
    (research_dir / "evil.py").write_text("print('x')\n", encoding="utf-8")
    status, _ = _get(host, port, "/data/research/evil.py")
    assert status == 403


def test_data_research_blocks_traversal(proxy_server):
    host, port, _store, _ = proxy_server
    status, _ = _get(host, port, "/data/research/../sessions/secret.jsonl")
    assert status in (403, 404)


def test_data_research_missing_is_404(proxy_server):
    host, port, _store, _ = proxy_server
    status, _ = _get(host, port, "/data/research/nope.html")
    assert status == 404
