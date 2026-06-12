"""v6 — proxy routes behind the Documents UI tab.

  • GET  /api/documents              → {documents[library], languages}
  • GET  /api/documents/<id>         → {document} (full content + versions)
  • POST /api/documents/<action>     → create/update/patch/delete/archive/
                                        restore/tidy → {ok, document?}

Reuses the proxy-server harness from tests/test_image_inline.py (fake project
root, ``core.proxy.__file__`` patched, an ephemeral ThreadingHTTPServer). The
DocumentsStore singleton is repointed at a temp JSONL file so nothing here
touches the real store, the GPU, or the network.
"""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest

from core import documents as dm


@pytest.fixture
def proxy_server(tmp_path, monkeypatch):
    from core import proxy as proxy_mod

    project_root = tmp_path / "TheAgent0"
    (project_root / "core").mkdir(parents=True)
    (project_root / "TheAgent0UI").mkdir(parents=True)
    (project_root / "TheAgent0UI" / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    monkeypatch.setattr(proxy_mod, "__file__", str(project_root / "core" / "proxy.py"))
    dm.reset_documents_store_for_tests(path=tmp_path / "documents.jsonl")

    server = proxy_mod.ThreadingHTTPServer(("127.0.0.1", 0), proxy_mod.ProxyHandler)
    host, port = server.server_address
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield host, port
    finally:
        server.shutdown()
        server.server_close()
        dm.reset_documents_store_for_tests()  # restore default singleton


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


# ── POST create → GET library / single ────────────────────────────────────
def test_create_then_library(proxy_server):
    host, port = proxy_server
    status, body = _post(host, port, "/api/documents/create",
                         {"content": "# Hello\n\nworld"})
    assert status == 200 and body["ok"] is True
    doc = body["document"]
    assert doc["title"] == "Hello" and doc["version_count"] == 1

    status, lib = _get(host, port, "/api/documents")
    assert status == 200
    assert any(d["id"] == doc["id"] for d in lib["documents"])
    assert "markdown" in lib["languages"]
    # Library rows are summaries (no heavy content blob).
    row = next(d for d in lib["documents"] if d["id"] == doc["id"])
    assert "content" not in row and "preview" in row


def test_get_single_document(proxy_server):
    host, port = proxy_server
    did = _post(host, port, "/api/documents/create",
                {"content": "full body here"})[1]["document"]["id"]
    status, body = _get(host, port, f"/api/documents/{did}")
    assert status == 200
    assert body["document"]["content"] == "full body here"
    assert isinstance(body["document"]["versions"], list)


def test_get_missing_document_404(proxy_server):
    host, port = proxy_server
    status, body = _get(host, port, "/api/documents/ghost123")
    assert status == 404 and "error" in body


# ── update / patch / restore ──────────────────────────────────────────────
def test_update_creates_version(proxy_server, monkeypatch):
    host, port = proxy_server
    # Force a new version regardless of timing by shrinking the coalesce window.
    monkeypatch.setattr(dm, "VERSION_COALESCE_SECONDS", 0.0)
    did = _post(host, port, "/api/documents/create",
                {"content": "one"})[1]["document"]["id"]
    status, body = _post(host, port, "/api/documents/update",
                         {"id": did, "content": "two"})
    assert status == 200 and body["document"]["version_count"] == 2
    assert body["document"]["content"] == "two"


def test_patch_metadata_only(proxy_server):
    host, port = proxy_server
    did = _post(host, port, "/api/documents/create",
                {"content": "body"})[1]["document"]["id"]
    status, body = _post(host, port, "/api/documents/patch",
                         {"id": did, "title": "Renamed", "language": "text"})
    assert status == 200
    assert body["document"]["title"] == "Renamed"
    assert body["document"]["language"] == "text"
    assert body["document"]["version_count"] == 1   # no new version


def test_restore_version(proxy_server, monkeypatch):
    host, port = proxy_server
    monkeypatch.setattr(dm, "VERSION_COALESCE_SECONDS", 0.0)
    did = _post(host, port, "/api/documents/create",
                {"content": "original"})[1]["document"]["id"]
    _post(host, port, "/api/documents/update", {"id": did, "content": "changed"})
    status, body = _post(host, port, "/api/documents/restore",
                         {"id": did, "version": 1})
    assert status == 200
    assert body["document"]["content"] == "original"
    assert body["document"]["version_count"] == 3


def test_update_missing_is_404(proxy_server):
    host, port = proxy_server
    status, body = _post(host, port, "/api/documents/update",
                         {"id": "ghost", "content": "x"})
    assert status == 404 and body["ok"] is False


# ── archive / delete ──────────────────────────────────────────────────────
def test_archive_hides_from_library(proxy_server):
    host, port = proxy_server
    did = _post(host, port, "/api/documents/create",
                {"content": "secret"})[1]["document"]["id"]
    _post(host, port, "/api/documents/archive", {"id": did})
    _, lib = _get(host, port, "/api/documents")
    assert all(d["id"] != did for d in lib["documents"])
    _, lib2 = _get(host, port, "/api/documents?archived=1")
    assert any(d["id"] == did for d in lib2["documents"])


def test_delete(proxy_server):
    host, port = proxy_server
    did = _post(host, port, "/api/documents/create",
                {"content": "temp"})[1]["document"]["id"]
    status, body = _post(host, port, "/api/documents/delete", {"id": did})
    assert status == 200 and body["ok"] is True
    assert _get(host, port, f"/api/documents/{did}")[0] == 404


# ── search / sort / tidy / unknown action ─────────────────────────────────
def test_library_search_and_sort(proxy_server):
    host, port = proxy_server
    _post(host, port, "/api/documents/create", {"title": "Apples", "content": "red fruit"})
    _post(host, port, "/api/documents/create", {"title": "Bananas", "content": "yellow fruit"})
    _, res = _get(host, port, "/api/documents?q=red")
    assert [d["title"] for d in res["documents"]] == ["Apples"]
    _, alpha = _get(host, port, "/api/documents?sort=alpha")
    titles = [d["title"] for d in alpha["documents"]]
    assert titles == sorted(titles, key=str.lower)


def test_tidy_dryrun(proxy_server):
    host, port = proxy_server
    jid = _post(host, port, "/api/documents/create",
                {"title": "test", "content": "x"})[1]["document"]["id"]
    status, body = _post(host, port, "/api/documents/tidy", {})
    assert status == 200 and body["ok"] is True
    assert jid in body["junk"] and body["removed"] == []
    # still present (dry-run)
    assert _get(host, port, f"/api/documents/{jid}")[0] == 200


def test_unknown_action_404(proxy_server):
    host, port = proxy_server
    status, body = _post(host, port, "/api/documents/frobnicate", {})
    assert status == 404 and body["ok"] is False
